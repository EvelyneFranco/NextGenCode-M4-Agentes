# Seguridad del HR Assistant

Este documento reúne **todos los controles de seguridad** que existen hoy en el código: los de las sesiones 1 a 4
(acceso, límite, compliance, logs sin contenido, despliegue) y los del reto de seguridad reforzada
(`spec/05-security-hardening.md`: PII, validación de salidas y de argumentos, inyección, auditoría y concurrencia).

Cada fila dice dónde está el control y con qué prueba se comprueba. Las pruebas corren con
`uv run python -m verification unit` (unitarias y Gherkin, cobertura de 80 % o más) y `contract`; las de navegador con
`verification e2e`; la versión publicada con `uv run python -m verification production <url>`. El detalle regla por
regla de producción está en `doc/compliance-checklist.md`; las decisiones, en los ADR 0010, 0011, 0012 y 0013.

## Modelo de amenazas, en breve

| Activo | Amenaza | Controles principales |
| --- | --- | --- |
| Directorio de empleados | Acceso sin permiso, abuso por volumen | Clave por cliente, límite por minuto, herramientas de solo lectura |
| Claves (`API_KEYS`, `CF_API_TOKEN`, `LANGSMITH_API_KEY`) | Fuga en log, traza, error o respuesta | `SecretStr`, errores sin valores, `[REDACTED:secret]` en la salida |
| PII ajena al directorio (correo, teléfono, tarjeta, CURP, RFC) | Que viaje al modelo, al log o a la traza | `redact_pii` en la entrada, en los argumentos auditados y en la salida |
| Comportamiento del agente | Inyección de instrucciones, argumentos abusivos | Bucle acotado, dos herramientas validadas, auditoría de frases conocidas |
| Servicio publicado | Configuración insegura, código sin probar | Compliance al arrancar, CI encadenado, publicación solo si el pipeline pasa |

## Controles

### Acceso y abuso (sesión 4)

| Control | Dónde está en el código | Cómo se comprueba |
| --- | --- | --- |
| `POST /ask` y `GET /metrics` exigen `X-API-Key`; sin clave válida responden `401 {"detail": "invalid or missing API key"}` sin gastar cupo | `app/main.py`: `ClientGuard.__call__`, dependencia de `ask` y `metrics_snapshot` | Escenario «A request without a valid key is rejected» (`production.feature`); `tests/test_main.py`; contratos de 401 |
| `GET /`, `GET /health` y `/docs` quedan abiertos y no exponen datos | `app/main.py`: `_add_open_routes` | Escenario «The page, the health check and the docs need no key» |
| Comparación de claves en tiempo constante contra todas las configuradas | `app/auth.py`: `ApiKeyAuth.client_for` (`hmac.compare_digest`, sin cortar al encontrar) | `tests/test_auth.py::test_each_key_maps_to_its_client_and_unknown_keys_to_none` |
| `API_KEYS` mal formada o con claves repetidas impide arrancar; el error dice posición y largo, nunca el valor | `app/auth.py`: `parse_api_keys`, `_parse_entry`, `_malformed`; `app/config.py`: `_production_values` (fuera de Pydantic) | `tests/test_auth.py::test_malformed_entry_error_names_its_position_and_length_but_never_its_value`; `tests/test_config.py::test_api_keys_never_appear_in_settings_or_startup_errors` |
| El nombre del cliente solo admite `[A-Za-z0-9._-]{1,64}` (evita inyección en logs) | `app/auth.py`: `VALID_CLIENT` | `tests/test_auth.py::test_malformed_api_keys_are_rejected` |
| Límite de `RATE_LIMIT_PER_MINUTE` peticiones por cliente; al pasarse, `429` con `Retry-After`; una petición negada no gasta cupo | `app/ratelimit.py`: `RateLimiter.check` (ventana fija de 60 s, `threading.Lock`); `app/main.py`: `ClientGuard` | `tests/test_ratelimit.py`; escenarios «A client over the limit waits until the minute is over» y «Two clients do not share the limit» |
| La página guarda la clave en `sessionStorage`, en un campo `password`, y pinta todo con `textContent` (sin HTML inyectable) | `app/static/index.html`: `readStoredKey`, `storeKey`, `sendQuestion`, `errorTextFor` | `tests/test_main.py::test_home_page_has_a_password_key_field_kept_in_session_storage`; pruebas de `e2e/` |

### Configuración y despliegue (sesión 4)

| Control | Dónde está en el código | Cómo se comprueba |
| --- | --- | --- |
| Con `ENVIRONMENT=production` no arranca con `MODEL_PROVIDER=fake`, sin `API_KEYS`, con claves de menos de 16 caracteres o con trazas que muestran entradas o salidas | `app/compliance.py`: `production_violations` y `enforce`, primera línea de `create_app` | Escenario «Production refuses to start with unsafe settings»; `tests/test_compliance.py` |
| Valores de configuración inválidos (`ENVIRONMENT`, proveedor, números, `MAX_ANSWER_CHARS`) impiden arrancar | `app/config.py`: validadores de `Settings` | `tests/test_config.py::test_invalid_environment_or_rate_limit_stops_startup`; `tests/test_security.py::test_max_answer_chars_defaults_to_2000_and_rejects_invalid_values` |
| `CF_API_TOKEN` es `SecretStr`; `LANGSMITH_API_KEY` nunca se guarda (solo se revisa que exista) | `app/config.py`: `Settings.cf_api_token`, `load_settings`; `app/tracing.py`: `Tracer.enabled` | `tests/test_config.py::test_booleans_are_parsed_and_api_key_is_never_stored` y `test_gateway_provider_requires_every_cloudflare_variable_and_hides_the_token` |
| Una falla del gateway se vuelve `502 model unavailable` sin el token ni el mensaje original | `app/gateway.py`; `app/main.py`: `_add_model_error_handler` (registra solo el tipo) | `tests/test_gateway.py::test_every_gateway_failure_becomes_model_unavailable_without_the_token` |
| Trazas apagadas por omisión; encendidas, ocultan entradas y salidas por omisión | `app/tracing.py`: `TracingOptions`, `Tracer` (fija `LANGSMITH_HIDE_INPUTS/OUTPUTS`) | `tests/test_tracing.py::test_tracing_does_nothing_without_api_key` y `test_tracing_wraps_with_langsmith_and_hides_inputs` |
| La imagen corre sin root, sin `.env`, pruebas ni documentos | `Dockerfile` (`USER hr`, uid 10001); `.dockerignore` | Trabajo `docker` de `.github/workflows/ci.yml` |
| Render publica solo si `style`, `unit`, `e2e` y `docker` pasan; los secretos van con `sync: false`; CI con `permissions: contents: read` | `render.yaml` (`autoDeployTrigger: checksPass`); `.github/workflows/ci.yml` (`needs`) | El pipeline mismo; `scripts/smoke.py` y `verification production <url>` tras publicar |

### Trazabilidad sin contenido (sesiones 2 a 4)

| Control | Dónde está en el código | Cómo se comprueba |
| --- | --- | --- |
| El log nunca lleva el texto de la pregunta ni de la respuesta, ni la clave: solo conteos, modelo, ruta, costo y cliente | `app/agent.py`: `Agent.run` (`agent_run`); `app/middleware.py`: `_finish` (nunca lee `X-API-Key`) | Escenario «The log never contains the question or the answer»; `tests/test_agent.py::test_agent_logs_audit_and_agent_run_without_any_text`; escenarios con «the key that was sent appears nowhere in the log» |
| `X-Request-ID` del cliente solo se acepta si cumple `[A-Za-z0-9._-]{1,128}`; si no, se genera un UUID | `app/middleware.py`: `resolve_request_id` | Escenario «An invalid client request id is replaced by a UUID» |
| Los errores internos responden `500` con `request_id` y registran solo el tipo de la excepción | `app/middleware.py`: `ObservabilityMiddleware.__call__` | Escenario «An internal error returns 500 with the request id and logs only the type» |
| Cada línea de log es un JSON de una sola línea aunque traiga saltos o caracteres no ASCII | `app/logs.py`: `JsonLogger.emit` | `tests/test_logs.py::test_non_ascii_and_newlines_stay_on_one_line` |
| Pregunta vacía o de más de `MAX_QUESTION_CHARS` responde `422` | `app/main.py`: `_validate_question` | Escenario «A validation error still carries the request id and is logged»; `tests/test_main.py` |
| El agente tiene tope de pasos (`AGENT_MAX_STEPS`) y solo dos herramientas de solo lectura | `app/agent.py`: `Agent._loop`, `_execute_tool`; ADR 0003 | `tests/test_agent.py::test_agent_reports_max_steps_when_it_stops_at_the_cap` |

### Seguridad reforzada (reto, `spec/05-security-hardening.md`)

| Control | Dónde está en el código | Cómo se comprueba |
| --- | --- | --- |
| Detección de PII (`email`, `phone`, `card` con Luhn, `curp`, `rfc`) que cambia cada coincidencia por `[REDACTED:<kind>]`; función pura, sin red ni log | `app/pii.py`: `redact_pii`, `luhn_valid`, `merge_kinds` | `tests/test_pii.py` (positivo y negativo por tipo, idempotencia) |
| Los datos del directorio (`EMP-0001`, nombres, departamentos, fechas, estado) nunca se ocultan | `app/pii.py`: patrones con largo exacto y límites de dígito | `tests/test_pii.py::test_directory_data_is_never_redacted`; escenario «Directory data is never redacted» |
| La pregunta y todo el `history` se limpian **antes** de enrutar; el modelo, el log, la auditoría y la traza solo ven el texto limpio; la petición no se rechaza | `app/main.py`: `_screen_input` y `_redact_history`, en `ask` antes de `route_question` | Escenarios «A question with an email is answered with the email redacted» y «The email never reaches the log or the trace» |
| Los argumentos del evento `audit` se registran sin PII y recortados a 64 caracteres | `app/agent.py`: `audit_args` | `tests/test_agent.py::test_agent_logs_audit_and_agent_run_without_any_text` |
| Mínimo privilegio en herramientas: `get_employee` acepta `EMP-####` o un nombre de 1 a 80 caracteres sin caracteres de control ni PII; `search_by_department`, solo un departamento del directorio; una herramienta desconocida se rechaza. Un argumento inválido no llega al directorio y el error no repite el valor | `app/guardrails.py`: `validate_tool_args`; `app/agent.py`: `Agent._call_tool` | `tests/test_guardrails.py::test_tool_arguments_follow_least_privilege`; `tests/test_security.py::test_invalid_tool_argument_never_reaches_the_directory`; escenario «An invalid tool argument never reaches the directory» |
| Validación de la salida, en este orden: secretos, PII, vacía y recorte a `MAX_ANSWER_CHARS`; corre dentro de la función trazada `agent_run` | `app/guardrails.py`: `validate_answer`; `app/agent.py`: `Agent._checked_loop` | `tests/test_guardrails.py`; `tests/test_security.py::test_empty_model_answer_is_replaced_by_the_safe_message` y `test_answer_is_trimmed_to_max_answer_chars` |
| Una clave de `API_KEYS` o `CF_API_TOKEN` nunca sale en la respuesta: se cambia por `[REDACTED:secret]`; los secretos vacíos se ignoran y el más largo se reemplaza primero | `app/guardrails.py`: `redact_secrets`; `app/config.py`: `Settings.secret_values` | Escenario «An API key in the model answer is redacted»; `tests/test_guardrails.py::test_secret_in_answer_is_redacted_and_not_mistaken_for_a_phone` y `test_long_answer_is_trimmed_without_leaving_part_of_a_secret`; `tests/test_security.py::test_secret_values_skip_empty_values_and_stay_out_of_repr` |
| Respuesta vacía se cambia por «No pude generar una respuesta segura.» | `app/guardrails.py`: `SAFE_FALLBACK_ANSWER` | `tests/test_guardrails.py::test_empty_answer_becomes_the_fixed_safe_message` |
| Detección de inyección de instrucciones con frases conocidas, sin acentos, mayúsculas ni espacios repetidos, en la pregunta y en el historial; solo se audita | `app/guardrails.py`: `detect_injection`, `INJECTION_PHRASES`; `app/main.py`: `_screen_input` | `tests/test_guardrails.py::test_injection_phrases_are_detected_regardless_of_accents_and_case` y `test_normal_questions_are_not_flagged_as_injection`; escenario «A prompt injection attempt is audited but not blocked» |
| Cada detección escribe un evento `security` (`request_id`, `client`, `stage`, `control`, `kinds`) en el log, suma en métricas y abre un span `security` en la traza; la firma no recibe texto, así que nunca escribe el valor | `app/security.py`: `SecurityAudit.record` | `tests/test_security.py::test_security_event_goes_to_log_metrics_and_trace_with_five_fields`; escenario «The email never reaches the log or the trace» |
| `GET /metrics` agrega `security` (conteo por control) sin cambiar las llaves anteriores | `app/metrics.py`: `Metrics.record_security`, `snapshot` | `tests/test_security.py::test_security_metrics_start_at_zero_and_keep_the_previous_keys` |
| `POST /ask` agrega `redactions` (tipos ocultados en la pregunta o en la respuesta, sin repetir) sin cambiar los demás campos | `app/main.py`: `ask`; `app/pii.py`: `merge_kinds` | `tests/test_security.py::test_ask_response_only_adds_an_empty_redactions_list`; escenarios con «the redactions contain» |
| 20 peticiones `/ask` simultáneas: todas `200`, `request_id` distintos, `/metrics` cuenta 20 y el límite sigue contando por cliente | `app/metrics.py`, `app/logs.py` y `app/ratelimit.py` (cada uno con su `threading.Lock`) | `tests/test_concurrency.py::test_twenty_simultaneous_questions_are_all_answered_and_counted` |

## Trade-offs

### Por qué expresiones regulares y no un modelo de PII

- **Determinismo y pruebas.** La misma entrada da siempre la misma salida. Cada tipo tiene un caso positivo y uno
  negativo, y la prueba de concurrencia puede contar exactamente 20 eventos `pii`. Con un modelo, el resultado varía
  y la prueba se vuelve probabilística.
- **Privacidad.** Detectar PII con un servicio externo obliga a mandarle el texto original, que es justo lo que se
  quiere evitar. `redact_pii` corre en el proceso y no usa red.
- **Costo y latencia.** Las expresiones regulares tardan microsegundos y no suman tokens ni afectan el SLO de p95.
- **Alcance acotado.** La spec define cinco tipos con formato fijo (correo, teléfono de 10 dígitos, tarjeta con
  Luhn, CURP y RFC). Para formatos fijos, una expresión regular es la herramienta exacta; un modelo serviría para PII
  sin formato (direcciones, nombres de terceros), que este producto no busca porque los nombres son el negocio.
- **Precio que se paga:** solo se detecta lo que tiene forma conocida. Ver abajo.

### Falsos positivos posibles

- **`phone`:** cualquier número de 10 dígitos seguidos (un folio, una cuenta, un número de pedido) se oculta. Los
  datos del directorio no tienen números así, por eso no afecta las respuestas actuales.
- **`card`:** un número de 13 a 19 dígitos que por casualidad pasa Luhn (aproximadamente 1 de cada 10 números al
  azar) se oculta aunque no sea tarjeta.
- **`rfc` y `curp`:** un código interno con 4 letras, 6 dígitos y 3 caracteres se ve como RFC. El `id` del
  directorio (`EMP-0001`) no coincide porque tiene guion y 4 dígitos.
- **Secretos:** si una clave de `API_KEYS` es una palabra común, cualquier respuesta que la contenga se marca como
  `[REDACTED:secret]`. La compliance exige 16 caracteres o más en producción, lo que lo hace muy improbable.
- **Inyección:** «actúa como» o «system prompt» pueden aparecer en una pregunta legítima («¿Quién actúa como jefe de
  Ventas?»). Como solo se audita, el costo es ruido en `security.injection`, no una respuesta negada.
- **Argumentos:** un nombre real de más de 80 caracteres se rechaza; el agente recibe el error y puede intentar con
  el `id`.

### Falsos negativos posibles

- **Teléfonos** con paréntesis (`(55) 1234 5678`), con puntos, con lada de otro país o con 8 dígitos locales.
- **Correos ofuscados** (`ana arroba empresa punto com`, `ana [at] empresa.com`).
- **Tarjetas** escritas con otros separadores, partidas en dos mensajes o que no pasan Luhn por un error de dedo.
- **RFC de persona moral** (12 caracteres) y CURP con errores de formato.
- **PII sin formato fijo:** direcciones, nombres de familiares, datos de salud. Están fuera del alcance de la spec.
- **Secretos** que el modelo devuelve transformados (en base64, separados por espacios o en partes): `redact_secrets`
  solo busca el valor exacto.
- **Inyección** con frases que no están en la lista, en otros idiomas, parafraseadas o codificadas.

### Por qué la inyección se audita y no se bloquea

- **El daño posible ya está acotado por diseño.** El agente solo tiene dos herramientas de solo lectura sobre el
  directorio, con argumentos validados por `validate_tool_args`, y un bucle con tope de pasos. Una inyección exitosa
  no puede escribir, borrar ni llamar a otro sistema; lo peor es una respuesta extraña, que además pasa por la
  validación de salida (secretos y PII).
- **Una lista de frases es fácil de evadir y fácil de disparar por error.** Bloquear con ella castigaría a usuarios
  legítimos (falsos positivos) sin detener a un atacante que cambia una palabra (falsos negativos).
- **La auditoría da la señal que sí sirve:** cuántos intentos hay, de qué cliente y cuándo. Con `security.injection`
  en `/metrics` y el evento en el log, una persona puede revocar la clave de un cliente o bajar su límite.
- **Si el producto crece** (herramientas que escriben, datos más sensibles), la decisión se revisa: el lugar para
  bloquear ya existe en `_screen_input`.

### Otros límites conocidos

- **El span `llm` de la traza lleva la salida cruda del modelo**, antes de `validate_answer`. Por eso, en producción,
  la compliance exige `HIDE_OUTPUTS=true` cuando las trazas están encendidas, y en `render.yaml` las trazas vienen
  apagadas.
- **El resultado de las herramientas** (datos del directorio) sí llega al modelo: es el negocio y no es PII según la
  spec.
- **Estado por proceso:** métricas y límite viven en memoria (ADR 0005 y ADR 0011). Con varias copias del servicio,
  el límite real es `RATE_LIMIT_PER_MINUTE` por copia; el contenedor arranca un solo proceso uvicorn.
- **Sin rotación ni revocación en caliente de claves:** cambiar `API_KEYS` requiere reiniciar el servicio.
