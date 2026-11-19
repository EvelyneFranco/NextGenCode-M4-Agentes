"""Agente del HR Assistant.

Ejecuta el bucle agente: envia mensajes al modelo, interpreta
la respuesta JSON, invoca herramientas y acumula pasos hasta
obtener una respuesta o alcanzar el tope.

Por cada herramienta emite un evento ``audit`` y al terminar un
``agent_run`` con conteos, modelo, ruta y costo; nunca registra el
texto de la pregunta ni de la respuesta. La corrida, cada llamada al
modelo y cada herramienta se trazan con el ``app.tracing.Tracer``
inyectado.

Desde la sesion 3 el modelo se elige por peticion (``run`` lo recibe)
y el costo de cada llamada se suma con la tabla de precios; una
llamada que vino de la cache del gateway cuenta en ``cached_calls`` y
no cuesta.

Desde la sesion 4 ``run`` recibe el ``client`` que hizo la pregunta y
lo pone en cada ``audit``, en ``agent_run`` y en los metadatos de las
trazas. La clave del cliente nunca llega al agente.

Desde el reto de seguridad reforzada:

- Antes de ejecutar una herramienta, ``validate_tool_args`` revisa sus
  argumentos (minimo privilegio). Un argumento invalido no llega al
  directorio: el modelo recibe un error legible, sin el valor, y se
  registra ``security`` con ``control=tool_args``.
- Los argumentos del evento ``audit`` pasan por ``redact_pii``.
- El nombre de herramienta que manda el modelo solo se repite en
  ``audit``, ``agent_run``, ``security`` y en el error si es conocido;
  si no, se registra ``unknown_tool`` (``safe_tool_name``).
- La respuesta final pasa por ``validate_answer`` dentro de la funcion
  trazada ``agent_run``, asi la salida de la traza ya va limpia.

:seealso: ADR 0003 sobre el bucle acotado del agente,
    ``spec/02-observability.md``, ``spec/03-costs.md`` y
    ``spec/05-security-hardening.md``.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from app.completion import TOOL_RESULT_PREFIX, Completion, Model
from app.directory import Directory
from app.guardrails import (
    CONTROL_TOOL_ARGS,
    TOOL_GET_EMPLOYEE,
    TOOL_SEARCH_BY_DEPARTMENT,
    validate_answer,
    validate_tool_args,
)
from app.logs import JsonLogger, NullLogger
from app.pii import redact_pii
from app.pricing import PriceTable
from app.security import STAGE_OUTPUT, Audit, NullAudit
from app.tracing import Tracer, TracingOptions

FALLBACK_MESSAGE = (
    "No pude completar la respuesta. "
    "Intenta con una pregunta más concreta."
)
STOPPED_ANSWER = "answer"
STOPPED_MAX_STEPS = "max_steps"
NO_REQUEST_ID = "-"
NO_ROUTE = "-"
NO_CLIENT = "-"
# Argumentos que se registran en ``audit`` por herramienta conocida.
AUDITED_ARGS = {
    "get_employee": ("employee_id",),
    "search_by_department": ("department",),
}
MAX_AUDIT_ARG_CHARS = 64
DEFAULT_MAX_ANSWER_CHARS = 2000
KNOWN_TOOLS = (TOOL_GET_EMPLOYEE, TOOL_SEARCH_BY_DEPARTMENT)
UNKNOWN_TOOL_KIND = "unknown_tool"
INVALID_ARGS_TEMPLATE = "argumento inválido para {tool}: {reason}"


def safe_tool_name(name: str) -> str:
    """Devuelve un nombre de herramienta seguro para el log y la traza.

    El nombre lo manda el modelo y puede traer cualquier texto: solo se
    repite si es una herramienta conocida; si no, se usa
    ``UNKNOWN_TOOL_KIND``.

    :param name: Nombre que pidio el modelo.
    :type name: str
    :return: El mismo nombre si es conocido o ``UNKNOWN_TOOL_KIND``.
    :rtype: str
    """
    return name if name in KNOWN_TOOLS else UNKNOWN_TOOL_KIND


def audit_args(tool: str, args: dict[str, Any]) -> dict[str, str]:
    """Filtra y recorta los argumentos de una herramienta para el log.

    Solo pasan los argumentos esperados de las herramientas conocidas,
    convertidos a texto, sin PII y truncados, para no filtrar datos de
    mas.

    :param tool: Nombre de la herramienta.
    :type tool: str
    :param args: Argumentos que pidio el modelo.
    :type args: dict[str, Any]
    :return: Argumentos seguros para registrar.
    :rtype: dict[str, str]
    """
    allowed = AUDITED_ARGS.get(tool, ())
    return {
        key: redact_pii(str(args[key])).text[:MAX_AUDIT_ARG_CHARS]
        for key in allowed
        if key in args
    }


def parse_decision(raw: str) -> dict[str, Any] | None:
    """Extrae la decision JSON del texto del modelo.

    Primero intenta el texto completo; si no es JSON (un modelo real
    puede agregar texto alrededor), usa el primer objeto JSON que
    aparezca.

    :param raw: Texto del modelo.
    :type raw: str
    :return: Diccionario de la decision o ``None`` si no hay uno.
    :rtype: dict[str, Any] | None
    """
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        data = _first_json_object(raw)
    return data if isinstance(data, dict) else None


def _first_json_object(raw: Any) -> Any:
    """Busca el primer objeto JSON dentro de un texto.

    :param raw: Texto del modelo.
    :type raw: Any
    :return: El objeto encontrado o ``None``.
    :rtype: Any
    """
    if not isinstance(raw, str):
        return None
    decoder = json.JSONDecoder()
    start = raw.find("{")
    while start != -1:
        try:
            return decoder.raw_decode(raw, start)[0]
        except json.JSONDecodeError:
            start = raw.find("{", start + 1)
    return None


@dataclass
class RunState:  # pylint: disable=too-many-instance-attributes
    """Estado de una corrida: modelo, ruta y lo acumulado.

    :param model: Modelo elegido para esta peticion.
    :type model: app.completion.Model
    :param route: Motivo del enrutador.
    :type route: str
    :param request_id: Id de la peticion, para log y trazas.
    :type request_id: str
    :param client: Cliente que pregunta, para log y trazas.
    :type client: str
    """

    model: Model
    route: str
    request_id: str = NO_REQUEST_ID
    client: str = NO_CLIENT
    input_tokens: int = 0
    output_tokens: int = 0
    cached_calls: int = 0
    cost_usd: float = 0.0
    tools_used: list[str] = field(default_factory=list)

    def add(self, completion: Completion, prices: PriceTable) -> None:
        """Suma tokens y costo de una llamada al modelo.

        Una llamada cacheada suma tokens y ``cached_calls``, pero no
        cuesta.

        :param completion: Respuesta de la llamada.
        :type completion: app.completion.Completion
        :param prices: Tabla de precios.
        :type prices: app.pricing.PriceTable
        """
        self.input_tokens += completion.input_tokens
        self.output_tokens += completion.output_tokens
        if completion.cached:
            self.cached_calls += 1
            return
        self.cost_usd += prices.cost(
            self.model.name, completion.input_tokens, completion.output_tokens
        )

    def trace_metadata(self, **extra: str) -> dict[str, str]:
        """Metadatos comunes de las trazas de esta corrida.

        :param extra: Campos adicionales, por ejemplo ``model``.
        :type extra: str
        :return: ``request_id``, ``client`` y los campos extra.
        :rtype: dict[str, str]
        """
        return {"request_id": self.request_id, "client": self.client, **extra}

    def result(self, answer: str, steps: int, stopped: str) -> dict[str, Any]:
        """Arma el diccionario de resultado de la corrida.

        :param answer: Texto de la respuesta.
        :type answer: str
        :param steps: Pasos usados.
        :type steps: int
        :param stopped: ``answer`` o ``max_steps``.
        :type stopped: str
        :return: Resultado de la corrida.
        :rtype: dict[str, Any]
        """
        return {
            "answer": answer,
            "steps": steps,
            "tools_used": list(self.tools_used),
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "stopped": stopped,
            "model": self.model.name,
            "route": self.route,
            "cached_calls": self.cached_calls,
            "cost_usd": self.cost_usd,
        }


class Agent:  # pylint: disable=too-many-instance-attributes
    """Agente que orquesta modelo y herramientas.

    En cada paso, envia los mensajes al modelo e interpreta
    su respuesta JSON. Si el modelo pide una herramienta, la
    ejecuta y agrega el resultado. Si responde directamente,
    el bucle termina. Si se alcanza el tope de pasos, devuelve
    el mensaje de fallback.

    :param directory: Directorio de empleados.
    :type directory: app.directory.Directory
    :param prices: Tabla de precios por modelo.
    :type prices: app.pricing.PriceTable
    :param max_steps: Tope de pasos del bucle.
    :type max_steps: int
    :param logger: Logger de eventos; si falta, no se registra nada.
    :type logger: app.logs.JsonLogger | None
    :param tracer: Trazador; si falta, no se traza nada.
    :type tracer: app.tracing.Tracer | None
    :param audit: Auditoria de seguridad; si falta, no se registra nada.
    :type audit: app.security.Audit | None
    :param secrets: Secretos que nunca salen en la respuesta.
    :type secrets: collections.abc.Iterable[str]
    :param max_answer_chars: Largo maximo de la respuesta.
    :type max_answer_chars: int
    """

    def __init__(  # pylint: disable=too-many-arguments,too-many-positional-arguments
        self,
        directory: Directory,
        prices: PriceTable,
        max_steps: int = 3,
        logger: JsonLogger | NullLogger | None = None,
        tracer: Tracer | None = None,
        audit: Audit | None = None,
        secrets: Iterable[str] = (),
        max_answer_chars: int = DEFAULT_MAX_ANSWER_CHARS,
    ) -> None:
        """Inicializa el agente con sus dependencias.

        Los tres ultimos parametros son opcionales, asi el agente se
        construye igual que en las sesiones anteriores.

        :param directory: Directorio de empleados.
        :type directory: app.directory.Directory
        :param prices: Tabla de precios por modelo.
        :type prices: app.pricing.PriceTable
        :param max_steps: Tope de pasos del bucle.
        :type max_steps: int
        :param logger: Logger de eventos.
        :type logger: app.logs.JsonLogger | None
        :param tracer: Trazador; por omision uno con trazas apagadas.
        :type tracer: app.tracing.Tracer | None
        :param audit: Auditoria de seguridad; por omision una que no
            registra nada.
        :type audit: app.security.Audit | None
        :param secrets: Claves de ``API_KEYS`` y ``CF_API_TOKEN``.
        :type secrets: collections.abc.Iterable[str]
        :param max_answer_chars: Largo maximo de la respuesta.
        :type max_answer_chars: int
        """
        self._directory = directory
        self._prices = prices
        self._max_steps = max_steps
        self._logger = logger if logger is not None else NullLogger()
        self._tracer = tracer if tracer is not None else Tracer(TracingOptions())
        self._audit = audit if audit is not None else NullAudit()
        self._secrets = tuple(secrets)
        self._max_answer_chars = max_answer_chars

    @property
    def audit(self) -> Audit:
        """Auditoria de seguridad del agente; la API la reusa en la entrada.

        :return: Auditoria inyectada o la que no registra nada.
        :rtype: app.security.Audit
        """
        return self._audit

    def run(  # pylint: disable=too-many-arguments
        self,
        question: str,
        history: list[dict[str, str]] | None = None,
        *,
        model: Model,
        request_id: str = NO_REQUEST_ID,
        route: str = NO_ROUTE,
        client: str = NO_CLIENT,
    ) -> dict[str, Any]:
        """Ejecuta el bucle del agente para una pregunta.

        :param question: Pregunta del usuario.
        :type question: str
        :param history: Historial previo (ya recortado por la ventana).
        :type history: list[dict[str, str]] | None
        :param model: Modelo elegido para esta peticion.
        :type model: app.completion.Model
        :param request_id: Id de la peticion, para log y trazas.
        :type request_id: str
        :param route: Motivo del enrutador.
        :type route: str
        :param client: Cliente que pregunta (nunca su clave), para
            ``audit``, ``agent_run`` y los metadatos de las trazas.
        :type client: str
        :return: Diccionario con answer, steps, tools_used,
            input_tokens, output_tokens, stopped, model, route,
            cached_calls, cost_usd y ``output_redactions`` (tipos
            ocultados en la respuesta).
        :rtype: dict[str, Any]
        :raises app.completion.ModelUnavailable: Si el modelo falla.
        """
        state = RunState(
            model=model, route=route, request_id=request_id, client=client
        )
        metadata = state.trace_metadata(model=model.name)
        traced_loop = self._tracer.traceable("agent_run", "chain", metadata)(
            self._checked_loop
        )
        result = traced_loop(question, history or [], state)
        self._logger.emit(
            "agent_run",
            request_id,
            client=client,
            question_chars=len(question),
            history_messages=len(history or []),
            steps=result["steps"],
            tools=list(result["tools_used"]),
            input_tokens=result["input_tokens"],
            output_tokens=result["output_tokens"],
            stopped=result["stopped"],
            model=result["model"],
            route=result["route"],
            cached_calls=result["cached_calls"],
            cost_usd=result["cost_usd"],
        )
        return result

    def _checked_loop(
        self,
        question: str,
        history: list[dict[str, str]],
        state: RunState,
    ) -> dict[str, Any]:
        """Corre el bucle y valida la respuesta final.

        Oculta secretos y PII, cambia una respuesta vacia por el mensaje
        fijo y recorta a ``max_answer_chars``. Cada hallazgo se registra
        en la auditoria con ``stage=output``.

        :param question: Pregunta del usuario (ya limpia).
        :type question: str
        :param history: Historial previo (ya limpio).
        :type history: list[dict[str, str]]
        :param state: Estado de la corrida.
        :type state: RunState
        :return: Resultado de la corrida con la respuesta validada y
            ``output_redactions``.
        :rtype: dict[str, Any]
        """
        result = self._loop(question, history, state)
        check = validate_answer(
            result["answer"], self._secrets, self._max_answer_chars
        )
        for control, kinds in check.findings:
            self._audit.record(
                state.request_id, state.client, STAGE_OUTPUT, control, kinds
            )
        result["answer"] = check.text
        result["output_redactions"] = list(check.kinds)
        return result

    def _loop(
        self,
        question: str,
        history: list[dict[str, str]],
        state: RunState,
    ) -> dict[str, Any]:
        """Corre las vueltas del bucle hasta responder o llegar al tope.

        :param question: Pregunta del usuario.
        :type question: str
        :param history: Historial previo de la conversacion.
        :type history: list[dict[str, str]]
        :param state: Estado de la corrida (con ``request_id`` y
            ``client``).
        :type state: RunState
        :return: Resultado de la corrida.
        :rtype: dict[str, Any]
        """
        metadata = state.trace_metadata(model=state.model.name)
        complete = self._tracer.traceable("llm", "llm", metadata)(
            state.model.complete
        )
        messages = [*history, {"role": "user", "content": question}]

        for step in range(1, self._max_steps + 1):
            completion = complete(messages)
            state.add(completion, self._prices)
            parsed = parse_decision(completion.text)

            if parsed is None:
                # JSON invalido, cuenta como paso fallido
                continue
            if "answer" in parsed:
                return state.result(str(parsed["answer"]), step, STOPPED_ANSWER)
            if "tool" in parsed:
                tool_name = str(parsed["tool"])
                args = parsed.get("args")
                tool_result = self._call_tool(
                    tool_name, args if isinstance(args, dict) else {}, state
                )
                # ``tools_used`` llega a ``agent_run``: nunca el texto crudo.
                state.tools_used.append(safe_tool_name(tool_name))
                messages.append(
                    {
                        "role": "user",
                        "content": TOOL_RESULT_PREFIX
                        + json.dumps(tool_result, ensure_ascii=False),
                    }
                )

        # Se alcanzo el tope sin respuesta
        return state.result(FALLBACK_MESSAGE, self._max_steps, STOPPED_MAX_STEPS)

    def _call_tool(self, name: str, args: dict[str, Any], state: RunState) -> Any:
        """Registra en ``audit``, valida e invoca una herramienta.

        Si ``validate_tool_args`` rechaza el argumento, no se llama al
        directorio: se registra ``security`` (``control=tool_args``) y
        el modelo recibe un error que no repite el valor.

        :param name: Nombre de la herramienta.
        :type name: str
        :param args: Argumentos de la herramienta.
        :type args: dict[str, Any]
        :param state: Estado de la corrida (``request_id`` y ``client``).
        :type state: RunState
        :return: Resultado de la herramienta o ``{"error": ...}``.
        :rtype: Any
        """
        # El nombre lo inventa el modelo: solo se repite si es conocido.
        tool = safe_tool_name(name)
        self._logger.emit(
            "audit",
            state.request_id,
            client=state.client,
            action="tool_call",
            tool=tool,
            args=audit_args(name, args),
        )
        reason = validate_tool_args(name, args, self._directory.departments)
        if reason is not None:
            self._audit.record(
                state.request_id,
                state.client,
                STAGE_OUTPUT,
                CONTROL_TOOL_ARGS,
                [tool],
            )
            return {"error": INVALID_ARGS_TEMPLATE.format(tool=tool, reason=reason)}
        metadata = state.trace_metadata()
        tool = self._tracer.traceable(name, "tool", metadata)(self._execute_tool)
        return tool(name, args)

    def _execute_tool(self, name: str, args: dict[str, Any]) -> Any:
        """Ejecuta la herramienta pedida sobre el directorio.

        Solo llega aqui una herramienta conocida con argumentos ya
        validados por ``validate_tool_args``.

        :param name: Nombre de la herramienta.
        :type name: str
        :param args: Argumentos de la herramienta.
        :type args: dict[str, Any]
        :return: Resultado de la herramienta.
        :rtype: Any
        """
        if name == TOOL_GET_EMPLOYEE:
            return self._directory.get_employee(
                str(args.get("employee_id", "")).strip()
            )
        return self._directory.search_by_department(
            str(args.get("department", ""))
        )
