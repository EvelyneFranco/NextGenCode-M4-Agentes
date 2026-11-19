"""Modelos del HR Assistant: simulado y fabrica por nombre.

Reexporta la interfaz ``Model``, ``Completion`` y ``ModelUnavailable``
de ``app.completion``, implementa el modelo simulado (fake) con conteo
aproximado de tokens y ``build_models``, que crea un modelo por cada
nombre configurado (``MODEL_SMALL`` y ``MODEL_LARGE``).

:seealso: ADR 0002 sobre la abstraccion del modelo y
    ``spec/03-costs.md``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable

import httpx

from app.completion import (
    TOOL_RESULT_PREFIX,
    Completion,
    Model,
    ModelUnavailable,
)
from app.config import PROVIDER_FAKE, PROVIDER_GATEWAY, Settings
from app.directory import Directory
from app.gateway import GATEWAY_TIMEOUT_SECONDS, GatewayModel
from app.text import fold_text

__all__ = [
    "Completion",
    "FakeModel",
    "Model",
    "ModelUnavailable",
    "TOOL_RESULT_PREFIX",
    "build_models",
]

# Departamentos de respaldo si no se inyectan los de los datos.
DEFAULT_DEPARTMENTS = (
    "Tecnología",
    "Ventas",
    "Finanzas",
)
HELP_MESSAGE = "Pregúntame por un empleado (EMP-0001) o un departamento."
FAKE_MODEL_NAME = "fake"


def build_models(
    settings: Settings,
    directory: Directory,
    client: httpx.Client | None = None,
) -> dict[str, Model]:
    """Crea un modelo por cada nombre configurado.

    Con ``MODEL_PROVIDER=fake`` son dos modelos simulados que responden
    igual pero se llaman como ``MODEL_SMALL`` y ``MODEL_LARGE`` (asi
    tienen precio). Con ``gateway`` son clientes de AI Gateway que
    comparten un ``httpx.Client``.

    :param settings: Configuracion validada.
    :type settings: app.config.Settings
    :param directory: Directorio, del que salen los departamentos.
    :type directory: app.directory.Directory
    :param client: Cliente HTTP para el gateway; si falta, se crea uno
        con el tiempo maximo por llamada.
    :type client: httpx.Client | None
    :return: Modelos por nombre.
    :rtype: dict[str, app.completion.Model]
    :raises ValueError: Si el proveedor no es soportado.
    """
    names = (settings.model_small, settings.model_large)
    if settings.model_provider == PROVIDER_FAKE:
        return {
            name: FakeModel(name=name, departments=directory.departments)
            for name in names
        }
    if settings.model_provider == PROVIDER_GATEWAY:
        shared = client or httpx.Client(timeout=GATEWAY_TIMEOUT_SECONDS)
        return {
            name: GatewayModel(name=name, settings=settings, client=shared)
            for name in names
        }
    raise ValueError(
        f"Proveedor de modelo no soportado: {settings.model_provider}"
    )


class FakeModel(Model):
    """Modelo simulado determinista, sin red ni costo.

    Decide con reglas sobre el ultimo mensaje del usuario:

    1. Si empieza con ``Resultado de la herramienta: ``
       responde con ``answer``.
    2. Si contiene un id ``EMP-XXXX`` pide ``get_employee``.
    3. Si menciona un departamento conocido pide
       ``search_by_department``.
    4. Si no, responde con un mensaje de ayuda.

    :seealso: ``spec/01-architecture.md`` seccion modelo simulado.

    :param name: Nombre del modelo, con el que se busca su precio.
    :type name: str
    :param departments: Departamentos que existen en los datos; si
        falta, se usa ``DEFAULT_DEPARTMENTS``.
    :type departments: collections.abc.Iterable[str] | None
    """

    def __init__(
        self,
        name: str = FAKE_MODEL_NAME,
        departments: Iterable[str] | None = None,
    ) -> None:
        """Guarda el nombre y los departamentos de la regla 3.

        :param name: Nombre del modelo.
        :type name: str
        :param departments: Departamentos de los datos.
        :type departments: collections.abc.Iterable[str] | None
        """
        self.name = name
        known = DEFAULT_DEPARTMENTS if departments is None else departments
        self._departments = sorted(set(known))

    def complete(self, messages: list[dict[str, str]]) -> Completion:
        """Aplica las reglas del modelo simulado.

        No guarda estado: los tokens viajan en el ``Completion``.

        :param messages: Lista de mensajes de la conversacion.
        :type messages: list[dict[str, str]]
        :return: Decision del modelo en JSON y sus tokens.
        :rtype: app.completion.Completion
        """
        response = self._decide(self._last_user_content(messages))
        return Completion(
            text=response,
            input_tokens=self._count_words(messages),
            output_tokens=len(response.split()),
            cached=False,
        )

    # -- Metodos internos -----------------------------------------------

    def _last_user_content(
        self, messages: list[dict[str, str]]
    ) -> str:
        """Obtiene el contenido del ultimo mensaje del usuario.

        :param messages: Lista de mensajes.
        :type messages: list[dict[str, str]]
        :return: Contenido del ultimo mensaje de rol 'user'.
        :rtype: str
        """
        for msg in reversed(messages):
            if msg.get("role") == "user":
                return msg.get("content", "")
        return ""

    def _decide(self, text: str) -> str:
        """Aplica las reglas en orden de prioridad.

        :param text: Texto del ultimo mensaje del usuario.
        :type text: str
        :return: Cadena JSON con la decision.
        :rtype: str
        """
        # Regla 1: resultado de herramienta
        if text.startswith(TOOL_RESULT_PREFIX):
            result_data = text[len(TOOL_RESULT_PREFIX):]
            return json.dumps(
                {"answer": f"Según el directorio: {result_data}"},
                ensure_ascii=False,
            )

        # Regla 2: id de empleado
        match = re.search(r"EMP-\d{4}", text)
        if match:
            return json.dumps(
                {
                    "tool": "get_employee",
                    "args": {"employee_id": match.group()},
                }
            )

        # Regla 3: departamento que existe en los datos (sin acentos)
        folded = fold_text(text)
        for dept in self._departments:
            if fold_text(dept) in folded:
                return json.dumps(
                    {
                        "tool": "search_by_department",
                        "args": {"department": dept},
                    },
                    ensure_ascii=False,
                )

        # Regla 4: mensaje de ayuda
        return json.dumps({"answer": HELP_MESSAGE}, ensure_ascii=False)

    def _count_words(
        self, messages: list[dict[str, str]]
    ) -> int:
        """Cuenta palabras en todos los mensajes (tokens aprox).

        :param messages: Lista de mensajes.
        :type messages: list[dict[str, str]]
        :return: Numero total de palabras.
        :rtype: int
        """
        total = 0
        for msg in messages:
            content = msg.get("content", "")
            total += len(content.split())
        return total
