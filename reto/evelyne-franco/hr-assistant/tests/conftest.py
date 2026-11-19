"""Fixtures compartidas para las pruebas del HR Assistant.

Provee la aplicacion FastAPI con todas sus dependencias inyectadas
(configuracion, modelo simulado, directorio de prueba, reloj falso,
logger sobre un ``StringIO`` y metricas en memoria) y clientes HTTP
listos para usar en cada prueba.

Desde la sesion 4, ``client`` manda la clave de prueba ``TEST_KEY`` en
``X-API-Key`` (asi las pruebas anteriores de ``/ask`` y ``/metrics``
siguen igual) y ``anonymous`` no manda ninguna. La aplicacion recibe
``auth`` y ``rate_limiter`` inyectados, con el mismo reloj falso.

:seealso: ``app.main.create_app`` y ``output/plan.md``
"""

import io
import json
import threading
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.auth import ApiKeyAuth, parse_api_keys
from app.config import Settings
from app.directory import Directory
from app.llm import TOOL_RESULT_PREFIX, Completion, FakeModel, ModelUnavailable
from app.logs import JsonLogger
from app.main import create_app
from app.metrics import Metrics
from app.pricing import PriceTable
from app.ratelimit import RateLimiter


# -- Datos de prueba -----------------------------------------------

SAMPLE_EMPLOYEES = [
    {
        "id": "EMP-0001",
        "name": "Ana Lopez",
        "department": "Tecnologia",
        "role": "Analista de datos",
        "start_date": "2019-03-04",
        "status": "activo",
    },
    {
        "id": "EMP-0002",
        "name": "Luis Perez",
        "department": "Ventas",
        "role": "Ejecutivo de cuenta",
        "start_date": "2021-07-19",
        "status": "activo",
    },
    {
        "id": "EMP-0003",
        "name": "Marta Ruiz",
        "department": "Tecnologia",
        "role": "Desarrolladora",
        "start_date": "2016-11-02",
        "status": "baja",
    },
    {
        "id": "EMP-0004",
        "name": "Jorge Diaz",
        "department": "Tecnologia",
        "role": "Lider tecnico",
        "start_date": "2022-01-10",
        "status": "activo",
    },
    {
        "id": "EMP-0005",
        "name": "Sofia Gomez",
        "department": "Finanzas",
        "role": "Contadora",
        "start_date": "2020-05-25",
        "status": "activo",
    },
]

FIXED_NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
SLO_P95_MS = 2000

# Sesion 3: nombres por omision de los modelos y copia de
# ``spec/model_prices.json`` (USD por millon de tokens).
MODEL_SMALL = "@cf/meta/llama-3.1-8b-instruct-fast"
MODEL_LARGE = "@cf/meta/llama-3.3-70b-instruct-fp8-fast"
SAMPLE_PRICES = {
    "currency": "USD",
    "unit": "per million tokens",
    "models": {
        "fake": {"input": 0.0, "output": 0.0},
        MODEL_SMALL: {"input": 0.045, "output": 0.384},
        MODEL_LARGE: {"input": 0.293, "output": 2.253},
    },
}

# Sesion 4: claves de prueba (no son secretas). ``client`` las manda en
# ``X-API-Key``; ``anonymous`` no manda ninguna.
TEST_CLIENT = "tester"
TEST_KEY = "tester-key-0123456789abcdef"
OTHER_CLIENT = "other"
OTHER_KEY = "other-key-0123456789abcdef"
TEST_API_KEYS = f"{TEST_CLIENT}:{TEST_KEY},{OTHER_CLIENT}:{OTHER_KEY}"
KEYS_BY_CLIENT = {TEST_CLIENT: TEST_KEY, OTHER_CLIENT: OTHER_KEY}
DEFAULT_RATE_LIMIT = 30


class FakeClock:
    """Reloj falso y determinista para las pruebas.

    ``now()`` siempre devuelve la misma hora UTC. ``monotonic()``
    avanza ``tick_seconds`` en cada llamada, de modo que una peticion
    medida con dos lecturas dura exactamente ``tick_seconds``.

    :param tick_seconds: Avance de ``monotonic()`` por llamada.
    :type tick_seconds: float
    """

    def __init__(self, tick_seconds=0.25):
        """Inicializa el reloj en el instante fijo.

        :param tick_seconds: Avance de ``monotonic()`` por llamada.
        :type tick_seconds: float
        """
        self._tick = tick_seconds
        self._mono = 100.0
        # Reto: la prueba de concurrencia lee el reloj desde varios hilos.
        self._lock = threading.Lock()

    def now(self):
        """Devuelve la hora fija en UTC.

        :return: Instante fijo con zona horaria UTC.
        :rtype: datetime.datetime
        """
        return FIXED_NOW

    def monotonic(self):
        """Devuelve el valor actual y avanza un tick.

        :return: Segundos de un contador monotono falso.
        :rtype: float
        """
        with self._lock:
            value = self._mono
            self._mono += self._tick
            return value

    def advance(self, seconds):
        """Adelanta el contador monotono, por ejemplo un minuto.

        :param seconds: Segundos que se adelanta el reloj.
        :type seconds: float
        """
        with self._lock:
            self._mono += seconds


class FailingModel:
    """Modelo que siempre truena, para probar el error 500.

    El mensaje de la excepcion contiene un texto sensible que
    nunca debe aparecer en el log.
    """

    name = "fake"
    SECRET = "secret-detail-do-not-log"

    def complete(self, messages):
        """Lanza siempre ``RuntimeError``.

        :param messages: Mensajes de la conversacion (ignorados).
        :type messages: list[dict[str, str]]
        :raises RuntimeError: Siempre.
        """
        raise RuntimeError(self.SECRET)


class UnavailableModel:
    """Modelo que simula un gateway caido, para probar el 502.

    :param name: Nombre del modelo que representa.
    :type name: str
    """

    SECRET = "gateway-detail-do-not-log"

    def __init__(self, name):
        """Guarda el nombre del modelo.

        :param name: Nombre del modelo.
        :type name: str
        """
        self.name = name

    def complete(self, messages):
        """Lanza siempre ``ModelUnavailable``.

        :param messages: Mensajes de la conversacion (ignorados).
        :type messages: list[dict[str, str]]
        :raises app.llm.ModelUnavailable: Siempre.
        """
        raise ModelUnavailable(self.SECRET)


class RecordingModel:
    """Modelo simulado que anota los mensajes de cada llamada.

    Delega en ``FakeModel`` y guarda una copia de los mensajes que
    recibe, para revisar la ventana de historial.

    :param name: Nombre del modelo que representa.
    :type name: str
    """

    def __init__(self, name):
        """Crea el modelo simulado interno.

        :param name: Nombre del modelo.
        :type name: str
        """
        self.name = name
        self.calls = []
        self._inner = FakeModel(name=name)

    def complete(self, messages):
        """Anota los mensajes y delega en el modelo simulado.

        :param messages: Mensajes de la conversacion.
        :type messages: list[dict[str, str]]
        :return: Respuesta del modelo simulado.
        :rtype: app.llm.Completion
        """
        self.calls.append([dict(m) for m in messages])
        return self._inner.complete(messages)


# -- Reto: dobles para la seguridad reforzada ------------------------

SAFE_FALLBACK_ANSWER = "No pude generar una respuesta segura."
LONG_NAME = "x" * 81
UNKNOWN_DEPARTMENT = "Marte"


def _answer(text):
    """Arma un ``Completion`` que responde ``text`` directamente.

    :param text: Texto de la respuesta.
    :type text: str
    :return: Respuesta del modelo en el formato JSON del agente.
    :rtype: app.llm.Completion
    """
    raw = json.dumps({"answer": text}, ensure_ascii=False)
    return Completion(text=raw, input_tokens=1, output_tokens=1, cached=False)


class SecretLeakingModel:
    """Modelo que filtra la clave ``TEST_KEY`` dentro de su respuesta.

    :param name: Nombre del modelo (debe tener precio).
    :type name: str
    """

    def __init__(self, name="fake"):
        """Guarda el nombre del modelo.

        :param name: Nombre del modelo.
        :type name: str
        """
        self.name = name

    def complete(self, messages):
        """Responde con un texto que contiene la clave de prueba.

        :param messages: Mensajes de la conversacion (ignorados).
        :type messages: list[dict[str, str]]
        :return: Respuesta que filtra ``TEST_KEY``.
        :rtype: app.llm.Completion
        """
        del messages
        return _answer(f"La clave del sistema es {TEST_KEY}")


class EmptyAnswerModel:
    """Modelo que responde solo con espacios.

    :param name: Nombre del modelo (debe tener precio).
    :type name: str
    """

    def __init__(self, name="fake"):
        """Guarda el nombre del modelo.

        :param name: Nombre del modelo.
        :type name: str
        """
        self.name = name

    def complete(self, messages):
        """Responde con una respuesta vacia.

        :param messages: Mensajes de la conversacion (ignorados).
        :type messages: list[dict[str, str]]
        :return: Respuesta sin texto util.
        :rtype: app.llm.Completion
        """
        del messages
        return _answer("   ")


class BadArgsModel:
    """Modelo que pide herramientas con argumentos invalidos.

    Pide, en orden, cada herramienta de ``requests``; cuando ya recibio
    un resultado por cada una, responde con el ultimo resultado, para
    que la prueba vea el error que leyo el modelo.

    :param name: Nombre del modelo (debe tener precio).
    :type name: str
    :param requests: Pares ``(herramienta, argumentos)`` a pedir.
    :type requests: list[tuple[str, dict]] | None
    """

    DEFAULT_REQUESTS = [
        ("get_employee", {"employee_id": LONG_NAME}),
        ("search_by_department", {"department": UNKNOWN_DEPARTMENT}),
    ]

    def __init__(self, name="fake", requests=None):
        """Guarda el nombre y las herramientas a pedir.

        :param name: Nombre del modelo.
        :type name: str
        :param requests: Pares ``(herramienta, argumentos)``.
        :type requests: list[tuple[str, dict]] | None
        """
        self.name = name
        self.requests = requests or self.DEFAULT_REQUESTS
        self.tool_results = []

    def complete(self, messages):
        """Pide la siguiente herramienta o responde con el ultimo resultado.

        :param messages: Mensajes de la conversacion.
        :type messages: list[dict[str, str]]
        :return: Decision del modelo.
        :rtype: app.llm.Completion
        """
        self.tool_results = [
            m["content"]
            for m in messages
            if m.get("content", "").startswith(TOOL_RESULT_PREFIX)
        ]
        done = len(self.tool_results)
        if done < len(self.requests):
            tool, args = self.requests[done]
            raw = json.dumps({"tool": tool, "args": args}, ensure_ascii=False)
            return Completion(text=raw, input_tokens=1, output_tokens=1, cached=False)
        return _answer(self.tool_results[-1])


class RecordingTracer:
    """Trazador habilitado que guarda cada span en memoria.

    Cumple la interfaz de ``app.tracing.Tracer``: ``enabled`` y
    ``traceable(name, run_type, metadata)``. Cada llamada a una funcion
    trazada agrega un diccionario a ``spans`` con el nombre, el tipo,
    los metadatos, los argumentos y el resultado.
    """

    enabled = True

    def __init__(self):
        """Inicializa la lista de spans y su candado."""
        self.spans = []
        self._lock = threading.Lock()

    def traceable(self, name, run_type, metadata=None):
        """Crea un decorador que anota cada llamada como un span.

        :param name: Nombre del span.
        :type name: str
        :param run_type: Tipo de corrida (``chain``, ``llm``, ``tool``).
        :type run_type: str
        :param metadata: Metadatos del span.
        :type metadata: dict | None
        :return: Decorador que anota y delega.
        :rtype: callable
        """

        def decorator(func):
            def wrapper(*args, **kwargs):
                result = func(*args, **kwargs)
                with self._lock:
                    self.spans.append(
                        {
                            "name": name,
                            "run_type": run_type,
                            "metadata": dict(metadata or {}),
                            "args": args,
                            "kwargs": kwargs,
                            "result": result,
                        }
                    )
                return result

            return wrapper

        return decorator

    def named(self, name):
        """Devuelve los spans con un nombre.

        :param name: Nombre del span, por ejemplo ``security``.
        :type name: str
        :return: Spans con ese nombre.
        :rtype: list[dict]
        """
        return [span for span in self.spans if span["name"] == name]

    def text(self):
        """Devuelve todo lo que vio el trazador como texto.

        :return: Representacion de todos los spans.
        :rtype: str
        """
        return repr(self.spans)


class Harness:  # pylint: disable=too-many-instance-attributes
    """Aplicacion de prueba con acceso a su log y sus metricas.

    :param client: Cliente HTTP sobre la aplicacion.
    :type client: fastapi.testclient.TestClient
    :param stream: Destino del log JSON.
    :type stream: io.StringIO
    :param metrics: Metricas inyectadas en la aplicacion.
    :type metrics: app.metrics.Metrics
    :param models: Modelos inyectados, por nombre.
    :type models: dict[str, app.llm.Model]
    :param settings: Configuracion de la aplicacion.
    :type settings: app.config.Settings
    :param anonymous: Cliente HTTP que no manda ``X-API-Key``.
    :type anonymous: fastapi.testclient.TestClient
    :param clock: Reloj falso compartido por la aplicacion.
    :type clock: FakeClock
    """

    def __init__(  # pylint: disable=too-many-arguments,too-many-positional-arguments
        self,
        client,
        stream,
        metrics,
        models=None,
        settings=None,
        anonymous=None,
        clock=None,
        app=None,
        tracer=None,
    ):
        """Guarda las piezas del arnes.

        Reto: ``app`` es la aplicacion (cada hilo de la prueba de
        concurrencia crea su propio ``TestClient`` sobre ella) y
        ``tracer`` el trazador inyectado, si lo hay.

        :param client: Cliente HTTP sobre la aplicacion.
        :type client: fastapi.testclient.TestClient
        :param stream: Destino del log JSON.
        :type stream: io.StringIO
        :param metrics: Metricas inyectadas.
        :type metrics: app.metrics.Metrics
        :param models: Modelos inyectados, por nombre.
        :type models: dict[str, app.llm.Model] | None
        :param settings: Configuracion de la aplicacion.
        :type settings: app.config.Settings | None
        """
        self.client = client
        self.stream = stream
        self.metrics = metrics
        self.models = models or {}
        self.settings = settings
        self.anonymous = anonymous
        self.clock = clock
        self.app = app
        self.tracer = tracer

    def post_as(self, client_name, path, **kwargs):
        """Hace un POST con la clave de un cliente de prueba.

        :param client_name: ``tester`` u ``other``.
        :type client_name: str
        :param path: Ruta de la peticion.
        :type path: str
        :return: Respuesta HTTP.
        :rtype: httpx.Response
        """
        headers = {"X-API-Key": KEYS_BY_CLIENT[client_name]}
        return self.anonymous.post(path, headers=headers, **kwargs)

    def log_text(self):
        """Devuelve todo el texto escrito en el log.

        :return: Contenido completo del log.
        :rtype: str
        """
        return self.stream.getvalue()

    def events(self, name=None):
        """Devuelve los eventos del log ya parseados.

        :param name: Si se indica, filtra por el campo ``event``.
        :type name: str | None
        :return: Lista de eventos como diccionarios.
        :rtype: list[dict]
        """
        lines = [ln for ln in self.log_text().splitlines() if ln]
        parsed = [json.loads(line) for line in lines]
        if name is None:
            return parsed
        return [e for e in parsed if e["event"] == name]


@pytest.fixture()
def employees_file(tmp_path):
    """Crea un archivo temporal con los datos de empleados.

    :return: Ruta al archivo JSON temporal.
    :rtype: pathlib.Path
    """
    path = tmp_path / "employees.json"
    path.write_text(
        json.dumps(SAMPLE_EMPLOYEES, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


@pytest.fixture()
def directory(employees_file):
    """Directorio de empleados cargado con ``SAMPLE_EMPLOYEES``.

    :param employees_file: Ruta al JSON temporal de empleados.
    :type employees_file: pathlib.Path
    :return: Instancia del directorio cargada.
    :rtype: app.directory.Directory
    """
    return Directory(str(employees_file))


@pytest.fixture()
def prices_file(tmp_path):
    """Crea un archivo temporal con la tabla de precios.

    :return: Ruta al JSON temporal de precios.
    :rtype: pathlib.Path
    """
    path = tmp_path / "model_prices.json"
    path.write_text(json.dumps(SAMPLE_PRICES), encoding="utf-8")
    return path


@pytest.fixture()
def prices(prices_file):
    """Tabla de precios cargada con ``SAMPLE_PRICES``.

    :param prices_file: Ruta al JSON temporal de precios.
    :type prices_file: pathlib.Path
    :return: Tabla de precios.
    :rtype: app.pricing.PriceTable
    """
    return PriceTable.from_file(str(prices_file), (MODEL_SMALL, MODEL_LARGE))


@pytest.fixture()
def build_harness(employees_file, prices_file):
    """Fabrica de aplicaciones de prueba con dependencias inyectadas.

    Inyecta un modelo por cada nombre (``MODEL_SMALL`` y
    ``MODEL_LARGE``). ``model`` usa la misma instancia para ambos;
    ``model_factory`` construye uno por nombre. Por omision son
    ``FakeModel`` con el nombre de cada modelo.

    :param employees_file: Ruta al JSON temporal de empleados.
    :type employees_file: pathlib.Path
    :param prices_file: Ruta al JSON temporal de precios.
    :type prices_file: pathlib.Path
    :return: Funcion que construye un ``Harness``.
    :rtype: callable
    """

    def _build(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals
        max_steps=3,
        tick_seconds=0.25,
        model=None,
        model_factory=None,
        rate_limit=DEFAULT_RATE_LIMIT,
        tracer=None,
        **overrides,
    ):
        # Reto: las claves de prueba tambien van en ``settings`` para que
        # ``secret_values()`` las conozca y la salida las oculte.
        overrides.setdefault("api_keys", parse_api_keys(TEST_API_KEYS))
        settings = Settings(
            employees_path=str(employees_file),
            prices_path=str(prices_file),
            agent_max_steps=max_steps,
            max_question_chars=2000,
            slo_p95_ms=SLO_P95_MS,
            **overrides,
        )
        directory = Directory(str(employees_file))
        if model is not None:
            models = {settings.model_small: model, settings.model_large: model}
        else:
            factory = model_factory or (
                lambda name: FakeModel(name=name, departments=directory.departments)
            )
            models = {
                name: factory(name)
                for name in (settings.model_small, settings.model_large)
            }
        clock = FakeClock(tick_seconds)
        stream = io.StringIO()
        metrics = Metrics(slo_p95_ms=SLO_P95_MS)
        application = create_app(
            settings=settings,
            models=models,
            prices=PriceTable.from_file(
                str(prices_file), (settings.model_small, settings.model_large)
            ),
            directory=directory,
            clock=clock,
            logger=JsonLogger(
                stream=stream, service="hr-assistant", clock=clock
            ),
            metrics=metrics,
            auth=ApiKeyAuth(parse_api_keys(TEST_API_KEYS)),
            rate_limiter=RateLimiter(rate_limit, clock),
            tracer=tracer,
        )
        client = TestClient(
            application,
            raise_server_exceptions=False,
            headers={"X-API-Key": TEST_KEY},
        )
        return Harness(
            client,
            stream,
            metrics,
            models,
            settings,
            anonymous=TestClient(application, raise_server_exceptions=False),
            clock=clock,
            app=application,
            tracer=tracer,
        )

    return _build


@pytest.fixture()
def harness(build_harness):
    """Aplicacion de prueba estandar (3 pasos, ticks de 250 ms).

    :param build_harness: Fabrica de aplicaciones de prueba.
    :type build_harness: callable
    :return: Arnes con cliente, log y metricas.
    :rtype: Harness
    """
    return build_harness()


@pytest.fixture()
def client(harness):
    """Cliente HTTP de prueba sobre la aplicacion inyectada.

    :param harness: Arnes de la aplicacion estandar.
    :type harness: Harness
    :return: Cliente HTTP sincronico.
    :rtype: fastapi.testclient.TestClient
    """
    return harness.client


@pytest.fixture()
def client_one_step(build_harness):
    """Cliente HTTP con tope de 1 paso para probar el fallback.

    :param build_harness: Fabrica de aplicaciones de prueba.
    :type build_harness: callable
    :return: Cliente HTTP sincronico.
    :rtype: fastapi.testclient.TestClient
    """
    return build_harness(max_steps=1).client
