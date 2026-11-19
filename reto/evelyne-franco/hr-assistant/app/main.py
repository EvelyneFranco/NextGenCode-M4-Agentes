"""Aplicacion FastAPI del HR Assistant.

Crea la aplicacion con ``create_app()`` usando inyeccion de
dependencias (configuracion, modelos por nombre, precios, directorio,
reloj, logger, metricas y trazador), define las rutas ``GET /``,
``GET /health``, ``GET /metrics`` y ``POST /ask``, y registra el
middleware de observabilidad.

Desde la sesion 4, ``POST /ask`` y ``GET /metrics`` exigen
``X-API-Key`` (``ClientGuard``: 401 sin clave valida y 429 si el
cliente pasa su limite por minuto). ``GET /``, ``GET /health`` y
``/docs`` siguen abiertos. Con ``ENVIRONMENT=production``,
``create_app`` corre las reglas de ``app.compliance`` antes de arrancar.

``POST /ask`` enruta la pregunta (palanca 1), recorta el historial
(palanca 2), corre el agente y responde con el modelo, el motivo, las
llamadas cacheadas y el costo. Si el modelo no responde, la API
contesta ``502 {"detail": "model unavailable", "request_id": ...}``.

Desde el reto de seguridad reforzada, antes de enrutar se audita la
inyeccion de instrucciones y se oculta la PII de la pregunta y del
historial (``_screen_input``); el agente valida la salida. La respuesta
agrega ``redactions`` (tipos ocultados, sin repetir) y ``/metrics`` la
llave ``security``.

:seealso: ADR 0001 sobre la API sin estado,
    ``spec/02-observability.md``, ``spec/03-costs.md`` y
    ``spec/05-security-hardening.md``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from app.agent import Agent
from app.auth import ApiKeyAuth
from app.clock import Clock, SystemClock
from app.compliance import enforce
from app.completion import Model, ModelUnavailable
from app.config import Settings, load_settings
from app.directory import Directory
from app.guardrails import (
    CONTROL_INJECTION,
    CONTROL_PII,
    KIND_PHRASE,
    detect_injection,
)
from app.llm import build_models
from app.logs import JsonLogger
from app.metrics import Metrics
from app.middleware import (
    CLIENT_STATE_KEY,
    ObservabilityMiddleware,
    request_id_of,
    started_of,
)
from app.pii import merge_kinds, redact_pii
from app.pricing import PriceTable
from app.ratelimit import RateLimiter
from app.routing import route_question, window_history
from app.security import STAGE_INPUT, Audit, SecurityAudit
from app.tracing import Tracer, TracingOptions

STATIC_DIR = Path(__file__).parent / "static"
# Campos de la respuesta de POST /ask (sesiones 1 y 3) mas ``request_id``.
SESSION_1_FIELDS = ("answer", "steps", "tools_used", "input_tokens", "output_tokens")
COST_FIELDS = ("model", "route", "cached_calls", "cost_usd")
ASK_FIELDS = SESSION_1_FIELDS + COST_FIELDS
MODEL_UNAVAILABLE_STATUS = 502
MODEL_UNAVAILABLE_DETAIL = "model unavailable"
API_KEY_HEADER = "X-API-Key"
UNAUTHORIZED_STATUS = 401
UNAUTHORIZED_DETAIL = "invalid or missing API key"
RATE_LIMITED_STATUS = 429
RATE_LIMITED_DETAIL = "rate limit exceeded"
RETRY_AFTER_HEADER = "Retry-After"


class ClientGuard:
    """Dependencia de FastAPI que exige clave y aplica el limite.

    Primero busca el cliente de ``X-API-Key``; sin clave valida responde
    401 (la peticion no gasta cupo). Despues deja el cliente en
    ``scope["state"]``, para el log ``request``, y consulta el limite;
    si se pasa responde 429 con ``Retry-After``. La clave nunca se
    guarda, ni se registra, ni aparece en el cuerpo de la respuesta.

    :param auth: Claves por cliente.
    :type auth: app.auth.ApiKeyAuth
    :param rate_limiter: Limite de peticiones por cliente.
    :type rate_limiter: app.ratelimit.RateLimiter
    """

    def __init__(self, auth: ApiKeyAuth, rate_limiter: RateLimiter) -> None:
        """Guarda la autenticacion y el limite.

        :param auth: Claves por cliente.
        :type auth: app.auth.ApiKeyAuth
        :param rate_limiter: Limite de peticiones por cliente.
        :type rate_limiter: app.ratelimit.RateLimiter
        """
        self._auth = auth
        self._rate_limiter = rate_limiter

    def __call__(self, request: Request) -> str:
        """Devuelve el cliente de la peticion o la rechaza.

        :param request: Peticion HTTP.
        :type request: fastapi.Request
        :return: Nombre del cliente autenticado.
        :rtype: str
        :raises HTTPException: 401 sin clave valida; 429 si el cliente
            paso su limite por minuto.
        """
        client = self._auth.client_for(request.headers.get(API_KEY_HEADER))
        if client is None:
            raise HTTPException(
                status_code=UNAUTHORIZED_STATUS, detail=UNAUTHORIZED_DETAIL
            )
        request.scope.setdefault("state", {})[CLIENT_STATE_KEY] = client
        # Se reusa la hora de llegada del middleware: una sola lectura
        # del reloj por peticion.
        decision = self._rate_limiter.check(client, now=started_of(request.scope))
        if not decision.allowed:
            raise HTTPException(
                status_code=RATE_LIMITED_STATUS,
                detail=RATE_LIMITED_DETAIL,
                headers={RETRY_AFTER_HEADER: str(decision.retry_after)},
            )
        return client


class AskRequest(BaseModel):
    """Cuerpo de la peticion POST /ask.

    :param question: Pregunta del usuario.
    :type question: str
    :param history: Historial opcional de la conversacion.
    :type history: list[dict[str, str]] | None
    """

    question: str
    history: Optional[list[dict[str, str]]] = None


def create_app(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    settings: Settings | None = None,
    models: dict[str, Model] | None = None,
    prices: PriceTable | None = None,
    directory: Directory | None = None,
    clock: Clock | None = None,
    logger: JsonLogger | None = None,
    metrics: Metrics | None = None,
    tracer: Tracer | None = None,
    auth: ApiKeyAuth | None = None,
    rate_limiter: RateLimiter | None = None,
) -> FastAPI:
    """Crea la aplicacion FastAPI con dependencias inyectadas.

    Cada dependencia que no se pase se construye a partir de
    ``settings``. Antes que nada corre ``app.compliance.enforce``: con
    ``ENVIRONMENT=production`` y una regla que falla, no arranca. Si
    faltan los precios de ``MODEL_SMALL`` o ``MODEL_LARGE``, tampoco.

    :param settings: Configuracion; por omision ``Settings()``.
    :type settings: app.config.Settings | None
    :param models: Modelos por nombre (``MODEL_SMALL`` y
        ``MODEL_LARGE``); por omision ``build_models(settings, ...)``.
    :type models: dict[str, app.completion.Model] | None
    :param prices: Tabla de precios; por omision se carga de
        ``settings.prices_path``.
    :type prices: app.pricing.PriceTable | None
    :param directory: Directorio de empleados.
    :type directory: app.directory.Directory | None
    :param clock: Reloj para la hora del log y la latencia.
    :type clock: app.clock.Clock | None
    :param logger: Logger de eventos JSON (salida estandar).
    :type logger: app.logs.JsonLogger | None
    :param metrics: Metricas del proceso.
    :type metrics: app.metrics.Metrics | None
    :param tracer: Trazador de LangSmith; por omision se construye con
        ``langsmith_tracing``, ``hide_inputs`` y ``hide_outputs`` de
        ``settings``.
    :type tracer: app.tracing.Tracer | None
    :param auth: Claves por cliente; por omision las de
        ``settings.api_keys``.
    :type auth: app.auth.ApiKeyAuth | None
    :param rate_limiter: Limite por cliente; por omision
        ``settings.rate_limit_per_minute`` con el reloj inyectado.
    :type rate_limiter: app.ratelimit.RateLimiter | None
    :return: Instancia configurada de la aplicacion.
    :rtype: fastapi.FastAPI
    :raises app.compliance.ComplianceError: Si es produccion y alguna
        regla de compliance falla.
    :raises ValueError: Si falta un modelo o un precio configurado.
    """
    settings = settings if settings is not None else Settings()
    enforce(settings)
    directory = directory or Directory(settings.employees_path)
    models = models or build_models(settings, directory)
    _require_models(models, settings)
    prices = prices or PriceTable.from_file(
        settings.prices_path, (settings.model_small, settings.model_large)
    )
    clock = clock or SystemClock()
    logger = logger or JsonLogger(
        stream=sys.stdout, service=settings.service_name, clock=clock
    )
    metrics = metrics or Metrics(slo_p95_ms=settings.slo_p95_ms)
    guard = Depends(
        ClientGuard(
            auth or ApiKeyAuth(settings.api_keys),
            rate_limiter or RateLimiter(settings.rate_limit_per_minute, clock),
        )
    )
    tracer = tracer or Tracer(TracingOptions.from_settings(settings))
    agent = Agent(
        directory=directory,
        prices=prices,
        max_steps=settings.agent_max_steps,
        logger=logger,
        tracer=tracer,
        # La misma auditoria sirve para la entrada (``ask``) y la salida.
        audit=SecurityAudit(logger, metrics, tracer),
        secrets=settings.secret_values(),
        max_answer_chars=settings.max_answer_chars,
    )

    application = FastAPI(title="HR Assistant")
    application.add_middleware(
        ObservabilityMiddleware, logger=logger, metrics=metrics, clock=clock
    )
    _add_model_error_handler(application, logger)
    _add_open_routes(application)

    @application.get("/metrics")
    def metrics_snapshot(client: str = guard) -> dict[str, Any]:
        """Devuelve las metricas de este proceso (exige clave).

        :param client: Cliente autenticado (lo resuelve ``ClientGuard``).
        :type client: str
        :return: Peticiones, error, latencia, tokens, costo y SLO.
        :rtype: dict[str, Any]
        """
        del client
        return metrics.snapshot()

    @application.post("/ask")
    def ask(
        body: AskRequest, request: Request, client: str = guard
    ) -> dict[str, Any]:
        """Procesa una pregunta del usuario (exige clave).

        :param body: Cuerpo de la peticion con la pregunta.
        :type body: AskRequest
        :param request: Peticion HTTP, para leer el ``request_id``.
        :type request: fastapi.Request
        :param client: Cliente autenticado (lo resuelve ``ClientGuard``).
        :type client: str
        :return: Respuesta del agente con tokens, modelo, ruta,
            llamadas cacheadas, costo, ``request_id`` y ``redactions``.
        :rtype: dict[str, Any]
        :raises HTTPException: Si la pregunta es vacia o
            excede el largo maximo (422).
        :raises app.completion.ModelUnavailable: Si el modelo falla
            (se convierte en 502).
        """
        question = _validate_question(body.question, settings.max_question_chars)
        request_id = request_id_of(request.scope)
        question, history, input_kinds = _screen_input(
            question, body.history or [], agent.audit, (request_id, client)
        )
        route = route_question(question, settings)
        result = agent.run(
            question,
            window_history(history, settings.history_window),
            model=models[route.model],
            request_id=request_id,
            route=route.reason,
            client=client,
        )
        metrics.record_tokens(result["input_tokens"], result["output_tokens"])
        metrics.record_cost(result["cost_usd"])
        response = {name: result[name] for name in ASK_FIELDS}
        response["request_id"] = request_id
        response["redactions"] = merge_kinds(
            input_kinds, result.get("output_redactions", [])
        )
        return response

    return application


def _add_model_error_handler(application: FastAPI, logger: JsonLogger) -> None:
    """Registra la respuesta 502 para cuando el modelo falla.

    :param application: Aplicacion a configurar.
    :type application: fastapi.FastAPI
    :param logger: Logger de eventos JSON.
    :type logger: app.logs.JsonLogger
    """

    @application.exception_handler(ModelUnavailable)
    def model_unavailable(request: Request, exc: ModelUnavailable) -> JSONResponse:
        """Responde 502 cuando el modelo no pudo contestar.

        Registra un evento ``error`` solo con el tipo: el mensaje no se
        escribe en el log.

        :param request: Peticion HTTP, para leer el ``request_id``.
        :type request: fastapi.Request
        :param exc: Falla del modelo.
        :type exc: app.completion.ModelUnavailable
        :return: ``{"detail": "model unavailable", "request_id": ...}``.
        :rtype: fastapi.responses.JSONResponse
        """
        request_id = request_id_of(request.scope)
        logger.emit("error", request_id, error_type=type(exc).__name__)
        return JSONResponse(
            {"detail": MODEL_UNAVAILABLE_DETAIL, "request_id": request_id},
            status_code=MODEL_UNAVAILABLE_STATUS,
        )


def _add_open_routes(application: FastAPI) -> None:
    """Registra las rutas que no piden clave: ``GET /`` y ``GET /health``.

    ``/docs`` tambien queda abierto (lo sirve FastAPI).

    :param application: Aplicacion a configurar.
    :type application: fastapi.FastAPI
    """

    @application.get("/", response_class=HTMLResponse)
    def home() -> HTMLResponse:
        """Sirve la pagina web del asistente.

        :return: Contenido HTML de la pagina estatica.
        :rtype: HTMLResponse
        """
        content = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        return HTMLResponse(content=content)

    @application.get("/health")
    def health() -> dict[str, Any]:
        """Verifica que la aplicacion esta corriendo.

        :return: Estado y PID del proceso.
        :rtype: dict[str, Any]
        """
        return {"status": "ok", "pid": os.getpid()}


def _require_models(models: dict[str, Model], settings: Settings) -> None:
    """Exige un modelo para ``MODEL_SMALL`` y otro para ``MODEL_LARGE``.

    :param models: Modelos por nombre.
    :type models: dict[str, app.completion.Model]
    :param settings: Configuracion con los nombres.
    :type settings: app.config.Settings
    :raises ValueError: Si falta alguno.
    """
    for name in (settings.model_small, settings.model_large):
        if name not in models:
            raise ValueError(f"Falta el modelo configurado: {name}")


def _redact_history(
    history: list[dict[str, str]],
) -> tuple[list[dict[str, str]], list[str]]:
    """Oculta la PII de cada valor del historial.

    :param history: Historial recibido.
    :type history: list[dict[str, str]]
    :return: Historial limpio y tipos ocultados, sin repetir.
    :rtype: tuple[list[dict[str, str]], list[str]]
    """
    found: list[list[str]] = []
    clean_history = []
    for message in history:
        clean_message = {}
        for key, value in message.items():
            result = redact_pii(str(value))
            found.append(result.kinds)
            clean_message[key] = result.text
        clean_history.append(clean_message)
    return clean_history, merge_kinds(*found)


def _screen_input(
    question: str,
    history: list[dict[str, str]],
    audit: Audit,
    who: tuple[str, str],
) -> tuple[str, list[dict[str, str]], list[str]]:
    """Revisa la entrada: detecta inyeccion y oculta la PII.

    La inyeccion solo se audita; la peticion sigue. La PII se oculta en
    la pregunta y en cada valor del historial completo. Desde aqui solo
    existe el texto limpio: el enrutador, el modelo, el log y la traza
    nunca ven el original.

    :param question: Pregunta ya validada.
    :type question: str
    :param history: Historial recibido.
    :type history: list[dict[str, str]]
    :param audit: Auditoria de seguridad.
    :type audit: app.security.Audit
    :param who: ``(request_id, client)`` de la peticion.
    :type who: tuple[str, str]
    :return: Pregunta limpia, historial limpio y tipos ocultados.
    :rtype: tuple[str, list[dict[str, str]], list[str]]
    """
    request_id, client = who
    texts = [question, *(str(m.get("content", "")) for m in history)]
    if any(detect_injection(text) for text in texts):
        audit.record(
            request_id, client, STAGE_INPUT, CONTROL_INJECTION, [KIND_PHRASE]
        )
    clean_question = redact_pii(question)
    clean_history, history_kinds = _redact_history(history)
    kinds = merge_kinds(clean_question.kinds, history_kinds)
    if kinds:
        audit.record(request_id, client, STAGE_INPUT, CONTROL_PII, kinds)
    return clean_question.text, clean_history, kinds


def _validate_question(raw: str, max_chars: int) -> str:
    """Limpia y valida la pregunta.

    :param raw: Texto recibido.
    :type raw: str
    :param max_chars: Largo maximo permitido.
    :type max_chars: int
    :return: Pregunta sin espacios en los extremos.
    :rtype: str
    :raises HTTPException: 422 si esta vacia o es muy larga.
    """
    question = raw.strip()
    if not question:
        raise HTTPException(
            status_code=422, detail="La pregunta no puede estar vacía."
        )
    if len(question) > max_chars:
        raise HTTPException(
            status_code=422,
            detail=f"La pregunta excede el límite de {max_chars} caracteres.",
        )
    return question


# Instancia por omision para ``uvicorn app.main:app``
app = create_app(load_settings(os.environ))
