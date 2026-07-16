import logging
import aiohttp  # Make sure aiohttp is imported at the top
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback

# We no longer need async_get_clientsession
from homeassistant.const import CONF_USERNAME, CONF_PASSWORD

from .const import (
    DOMAIN,
    BASE_URL,
    BASE_URL_DHW,
    API_QUERIES_GET,
    API_QUERIES_GET_DHW,
    DEFAULT_SCAN_INTERVAL,
    REQUEST_TIMEOUT,
)

from .config_flow_modbus import (
    CONNECTION_TYPE_CLOUD,
    CONNECTION_TYPE_MODBUS,
    MODBUS_TRANSPORT_TCP,
    MODBUS_TRANSPORT_RTU,
    validate_modbus_connection,
    get_connection_type_schema,
    get_modbus_transport_schema,
    get_modbus_tcp_schema,
    get_modbus_rtu_schema,
)

from .entity_cleanup import (
    disable_mode_specific_entities,
    enable_mode_specific_entities,
)

_LOGGER = logging.getLogger(__name__)

SENSITIVE_KEYS = [CONF_USERNAME, CONF_PASSWORD]


def sanitize_user_input(user_input: dict) -> dict:
    """
    Sanitizes user input by redacting sensitive information for logging purposes.
    """
    return {
        key: ("[REDACTED]" if key in SENSITIVE_KEYS else value)
        for key, value in user_input.items()
    }


async def _probe_cloud_endpoint(
    base_url: str,
    menu_query: dict,
    username: str,
    password: str,
    phonegap_version: str,
) -> bool:
    """Validate one cloud endpoint using both supported login methods."""
    auth = aiohttp.BasicAuth(username, password)
    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
    headers = {
        "phonegap": phonegap_version,
        "X-Requested-With": "XMLHttpRequest",
    }
    login_url = (
        "https://cloud.kronoterm.com/dhws/?login=1"
        if "/dhws/" in base_url
        else "https://cloud.kronoterm.com/?login=1"
    )

    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(
                base_url,
                auth=auth,
                params=menu_query,
                headers=headers,
                timeout=timeout,
            ) as response:
                if response.status == 200:
                    payload = await response.json(content_type=None)
                    if "hp_id" in payload:
                        return True
        except (aiohttp.ClientError, ValueError):
            pass

        web_headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": "https://cloud.kronoterm.com",
            "Referer": login_url,
            "User-Agent": "Mozilla/5.0",
        }
        try:
            async with session.post(
                login_url,
                data={"username": username, "password": password},
                headers=web_headers,
                timeout=timeout,
                allow_redirects=True,
            ) as response:
                if response.status not in (200, 302):
                    return False

            async with session.get(
                base_url,
                params=menu_query,
                headers={
                    "X-Requested-With": "XMLHttpRequest",
                    "User-Agent": "Mozilla/5.0",
                },
                timeout=timeout,
            ) as response:
                if response.status != 200:
                    return False
                payload = await response.json(content_type=None)
                return "hp_id" in payload
        except (aiohttp.ClientError, ValueError):
            return False


async def validate_credentials(
    data: dict, preferred_type: str = "auto"
) -> tuple[str | None, str | None]:
    """
    Validate the credentials by attempting a lightweight API call.
    Returns (error_code, system_type) on failure/success.

    preferred_type: "auto" | "cloud" | "dhw"
    """
    username = data[CONF_USERNAME]
    password = data[CONF_PASSWORD]
    endpoints = {
        "cloud": (BASE_URL, API_QUERIES_GET["menu"], "1.5.0"),
        "dhw": (BASE_URL_DHW, API_QUERIES_GET_DHW["menu"], "1.0.7"),
    }
    candidates = (preferred_type,) if preferred_type in endpoints else ("cloud", "dhw")

    for system_type in candidates:
        _LOGGER.debug("Validating Kronoterm %s cloud endpoint", system_type)
        base_url, menu_query, phonegap_version = endpoints[system_type]
        if await _probe_cloud_endpoint(
            base_url,
            menu_query,
            username,
            password,
            phonegap_version,
        ):
            return None, system_type
    return "invalid_auth", None


class KronotermConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """
    Handles the configuration flow for the Kronoterm integration.
    """

    VERSION = 3

    def __init__(self) -> None:
        """Initialize the config flow."""
        self.connection_type = None
        self.modbus_transport = MODBUS_TRANSPORT_TCP
        self.reconfig_entry = None

    async def async_step_user(self, user_input: dict | None = None):
        """Handle a flow initialized by the user - choose connection type."""
        _LOGGER.debug("Starting user step in KronotermConfigFlow")

        if user_input is not None:
            # Store connection type and move to appropriate step
            self.connection_type = user_input.get(
                "connection_type", CONNECTION_TYPE_CLOUD
            )
            _LOGGER.debug("Connection type selected: %s", self.connection_type)

            if self.connection_type == CONNECTION_TYPE_MODBUS:
                return await self.async_step_modbus_transport()
            else:
                return await self.async_step_cloud()

        # Show connection type selection
        return self.async_show_form(
            step_id="user",
            data_schema=get_connection_type_schema(),
        )

    async def async_step_cloud(self, user_input: dict | None = None):
        """Handle cloud API configuration."""
        _LOGGER.debug("Starting cloud API configuration step")
        errors: dict[str, str] = {}

        if user_input is not None:
            sanitized_input = sanitize_user_input(user_input)
            _LOGGER.debug("User input received: %s", sanitized_input)

            preferred_type = user_input.get("cloud_type", "auto")
            # Validate credentials
            error_code, system_type = await validate_credentials(
                user_input, preferred_type
            )

            if not error_code and system_type:
                # Auth success, add connection type and create entry
                user_input["connection_type"] = CONNECTION_TYPE_CLOUD
                user_input["system_type"] = system_type  # Store system type (cloud/dhw)

                title = "Pompa di calore Kronoterm (Cloud)"
                if system_type == "dhw":
                    title = "Pompa di calore ACS Kronoterm (Cloud)"

                return self.async_create_entry(title=title, data=user_input)
            elif error_code:
                # Auth failed, set error and show form again
                errors["base"] = error_code
            else:
                errors["base"] = "unknown"

        cloud_schema = vol.Schema(
            {
                vol.Required(CONF_USERNAME): str,
                vol.Required(CONF_PASSWORD): str,
                vol.Required("cloud_type", default="auto"): vol.In(
                    {
                        "auto": "Automatic detection",
                        "cloud": "Heating heat pump",
                        "dhw": "Sanitary water heat pump",
                    }
                ),
            }
        )

        return self.async_show_form(
            step_id="cloud", data_schema=cloud_schema, errors=errors
        )

    async def async_step_modbus_transport(self, user_input: dict | None = None):
        """Choose Modbus transport (TCP/RTU)."""
        if user_input is not None:
            self.modbus_transport = user_input.get("transport", MODBUS_TRANSPORT_TCP)
            if self.modbus_transport == MODBUS_TRANSPORT_RTU:
                return await self.async_step_modbus_rtu()
            return await self.async_step_modbus_tcp()

        return self.async_show_form(
            step_id="modbus_transport",
            data_schema=get_modbus_transport_schema(),
        )

    async def async_step_modbus_tcp(self, user_input: dict | None = None):
        """Handle Modbus TCP configuration."""
        _LOGGER.debug("Starting Modbus TCP configuration step")
        errors: dict[str, str] = {}

        if user_input is not None:
            _LOGGER.debug("Modbus TCP config received: %s", user_input)
            user_input["transport"] = MODBUS_TRANSPORT_TCP

            # Validate Modbus connection
            error_code = await validate_modbus_connection(user_input)
            if not error_code:
                user_input["connection_type"] = CONNECTION_TYPE_MODBUS
                return self.async_create_entry(
                    title="Pompa di calore Kronoterm (Modbus)",
                    data=user_input,
                )
            errors["base"] = error_code

        return self.async_show_form(
            step_id="modbus_tcp",
            data_schema=get_modbus_tcp_schema(),
            errors=errors,
        )

    async def async_step_modbus_rtu(self, user_input: dict | None = None):
        """Handle Modbus RTU configuration."""
        _LOGGER.debug("Starting Modbus RTU configuration step")
        errors: dict[str, str] = {}

        if user_input is not None:
            _LOGGER.debug("Modbus RTU config received: %s", user_input)
            user_input["transport"] = MODBUS_TRANSPORT_RTU

            error_code = await validate_modbus_connection(user_input)
            if not error_code:
                user_input["connection_type"] = CONNECTION_TYPE_MODBUS
                return self.async_create_entry(
                    title="Pompa di calore Kronoterm (Modbus)",
                    data=user_input,
                )
            errors["base"] = error_code

        return self.async_show_form(
            step_id="modbus_rtu",
            data_schema=get_modbus_rtu_schema(),
            errors=errors,
        )

    async def async_step_reconfigure(self, user_input: dict | None = None):
        """Handle reconfiguration of an existing entry."""
        self.reconfig_entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        _LOGGER.info(
            "Starting reconfiguration for entry %s (current type: %s)",
            self.reconfig_entry.entry_id,
            self.reconfig_entry.data.get("connection_type", "cloud"),
        )
        return await self.async_step_reconfigure_connection_type(user_input)

    async def async_step_reconfigure_connection_type(
        self, user_input: dict | None = None
    ):
        """Choose new connection type during reconfiguration."""
        if user_input is not None:
            self.connection_type = user_input.get(
                "connection_type", CONNECTION_TYPE_CLOUD
            )
            _LOGGER.debug(
                "Reconfigure: New connection type selected: %s", self.connection_type
            )

            if self.connection_type == CONNECTION_TYPE_MODBUS:
                return await self.async_step_reconfigure_modbus_transport()
            else:
                return await self.async_step_reconfigure_cloud()

        current_type = self.reconfig_entry.data.get(
            "connection_type", CONNECTION_TYPE_CLOUD
        )
        current_type_name = (
            "Modbus" if current_type == CONNECTION_TYPE_MODBUS else "Cloud"
        )

        return self.async_show_form(
            step_id="reconfigure_connection_type",
            data_schema=get_connection_type_schema(),
            description_placeholders={"current_type": current_type_name},
        )

    async def async_step_reconfigure_cloud(self, user_input: dict | None = None):
        """Handle cloud API reconfiguration."""
        _LOGGER.debug("Reconfiguring to Cloud API")
        errors: dict[str, str] = {}

        if user_input is not None:
            sanitized_input = sanitize_user_input(user_input)
            _LOGGER.debug("Reconfigure cloud input: %s", sanitized_input)

            preferred_type = user_input.get("cloud_type", "auto")
            # Validate credentials
            error_code, system_type = await validate_credentials(
                user_input, preferred_type
            )

            if not error_code and system_type:
                # Auth success, update the entry
                user_input["connection_type"] = CONNECTION_TYPE_CLOUD
                user_input["system_type"] = system_type

                title = "Pompa di calore Kronoterm (Cloud)"
                if system_type == "dhw":
                    title = "Pompa di calore ACS Kronoterm (Cloud)"

                self.hass.config_entries.async_update_entry(
                    self.reconfig_entry, data=user_input, title=title
                )

                # Disable Modbus-only entities, re-enable Cloud entities
                await disable_mode_specific_entities(
                    self.hass, self.reconfig_entry.entry_id, "cloud"
                )
                await enable_mode_specific_entities(
                    self.hass, self.reconfig_entry.entry_id, "cloud"
                )

                # Reload the entry to apply changes
                await self.hass.config_entries.async_reload(
                    self.reconfig_entry.entry_id
                )
                return self.async_abort(reason="reconfigure_successful")
            else:
                errors["base"] = error_code

        # Pre-fill current credentials if switching from cloud
        current_data = self.reconfig_entry.data
        default_username = current_data.get(CONF_USERNAME, "")
        default_password = current_data.get(CONF_PASSWORD, "")

        cloud_schema = vol.Schema(
            {
                vol.Required(CONF_USERNAME, default=default_username): str,
                vol.Required(CONF_PASSWORD, default=default_password): str,
                vol.Required(
                    "cloud_type", default=current_data.get("cloud_type", "auto")
                ): vol.In(
                    {
                        "auto": "Automatic detection",
                        "cloud": "Heating heat pump",
                        "dhw": "Sanitary water heat pump",
                    }
                ),
            }
        )

        return self.async_show_form(
            step_id="reconfigure_cloud", data_schema=cloud_schema, errors=errors
        )

    async def async_step_reconfigure_modbus_transport(
        self, user_input: dict | None = None
    ):
        """Select Modbus transport during reconfiguration."""
        current_data = self.reconfig_entry.data
        current_transport = current_data.get("transport", MODBUS_TRANSPORT_TCP)

        if user_input is not None:
            self.modbus_transport = user_input.get("transport", MODBUS_TRANSPORT_TCP)
            if self.modbus_transport == MODBUS_TRANSPORT_RTU:
                return await self.async_step_reconfigure_modbus_rtu()
            return await self.async_step_reconfigure_modbus_tcp()

        return self.async_show_form(
            step_id="reconfigure_modbus_transport",
            data_schema=get_modbus_transport_schema(current_transport),
        )

    async def async_step_reconfigure_modbus_tcp(self, user_input: dict | None = None):
        """Handle Modbus TCP reconfiguration."""
        _LOGGER.debug("Reconfiguring to Modbus TCP")
        errors: dict[str, str] = {}

        if user_input is not None:
            _LOGGER.debug("Reconfigure modbus TCP input: %s", user_input)
            user_input["transport"] = MODBUS_TRANSPORT_TCP
            error_code = await validate_modbus_connection(user_input)
            if not error_code:
                user_input["connection_type"] = CONNECTION_TYPE_MODBUS
                self.hass.config_entries.async_update_entry(
                    self.reconfig_entry,
                    data=user_input,
                    title="Pompa di calore Kronoterm (Modbus)",
                )

                await disable_mode_specific_entities(
                    self.hass, self.reconfig_entry.entry_id, "modbus"
                )
                await enable_mode_specific_entities(
                    self.hass, self.reconfig_entry.entry_id, "modbus"
                )

                await self.hass.config_entries.async_reload(
                    self.reconfig_entry.entry_id
                )
                return self.async_abort(reason="reconfigure_successful")
            errors["base"] = error_code

        current_data = self.reconfig_entry.data
        return self.async_show_form(
            step_id="reconfigure_modbus_tcp",
            data_schema=get_modbus_tcp_schema(current_data),
            errors=errors,
        )

    async def async_step_reconfigure_modbus_rtu(self, user_input: dict | None = None):
        """Handle Modbus RTU reconfiguration."""
        _LOGGER.debug("Reconfiguring to Modbus RTU")
        errors: dict[str, str] = {}

        if user_input is not None:
            _LOGGER.debug("Reconfigure modbus RTU input: %s", user_input)
            user_input["transport"] = MODBUS_TRANSPORT_RTU
            error_code = await validate_modbus_connection(user_input)
            if not error_code:
                user_input["connection_type"] = CONNECTION_TYPE_MODBUS
                self.hass.config_entries.async_update_entry(
                    self.reconfig_entry,
                    data=user_input,
                    title="Pompa di calore Kronoterm (Modbus)",
                )

                await disable_mode_specific_entities(
                    self.hass, self.reconfig_entry.entry_id, "modbus"
                )
                await enable_mode_specific_entities(
                    self.hass, self.reconfig_entry.entry_id, "modbus"
                )

                await self.hass.config_entries.async_reload(
                    self.reconfig_entry.entry_id
                )
                return self.async_abort(reason="reconfigure_successful")
            errors["base"] = error_code

        current_data = self.reconfig_entry.data
        return self.async_show_form(
            step_id="reconfigure_modbus_rtu",
            data_schema=get_modbus_rtu_schema(current_data),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> "KronotermOptionsFlowHandler":
        """Get the options flow for this integration."""
        return KronotermOptionsFlowHandler()


class KronotermOptionsFlowHandler(config_entries.OptionsFlow):
    """
    Handles the options flow for the Kronoterm integration.
    Allows updating credentials, scan interval, and other settings.
    """

    async def async_step_init(self, user_input: dict | None = None):
        """Handle the initial step of the options flow."""
        _LOGGER.debug("Starting options flow init step")
        errors: dict[str, str] = {}

        if user_input is not None:
            # Validate credentials, as they might have been changed
            # We no longer pass self.hass
            error_code, system_type = await validate_credentials(user_input)

            if not error_code and system_type:
                _LOGGER.debug("Options saved: %s", sanitize_user_input(user_input))
                # Update system_type in case it changed (e.g. diff cloud endpoint)
                user_input["system_type"] = system_type
                # Save the validated input (including any new credentials)
                return self.async_create_entry(title="", data=user_input)
            elif error_code:
                _LOGGER.warning(
                    "Failed to save options: credentials invalid (%s)", error_code
                )
                errors["base"] = error_code
            else:
                errors["base"] = "unknown"

        # Get current values from options, falling back to data (for credentials)
        # or defaults (for other settings)
        current_options = self.config_entry.options
        current_data = self.config_entry.data

        username = current_options.get(
            CONF_USERNAME, current_data.get(CONF_USERNAME, "")
        )
        password = current_options.get(
            CONF_PASSWORD, current_data.get(CONF_PASSWORD, "")
        )
        scan_interval = current_options.get("scan_interval", DEFAULT_SCAN_INTERVAL)

        # Build the schema with current values as defaults
        options_schema = vol.Schema(
            {
                vol.Required(CONF_USERNAME, default=username): str,
                vol.Required(CONF_PASSWORD, default=password): str,
                vol.Optional("scan_interval", default=scan_interval): vol.All(
                    vol.Coerce(int), vol.Range(min=1, max=60)
                ),
            }
        )

        return self.async_show_form(
            step_id="init", data_schema=options_schema, errors=errors
        )
