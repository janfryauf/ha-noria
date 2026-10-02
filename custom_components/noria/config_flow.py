"""Config flow for Noria Online Monitoring: user, reauth, reconfigure."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import TextSelector, TextSelectorConfig, TextSelectorType
import voluptuous as vol

from .api import NoriaAuthError, NoriaClient, NoriaError, NoriaForbiddenError
from .const import DOMAIN, LOGGER

USERNAME_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT, autocomplete="username"))
PASSWORD_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD, autocomplete="current-password"))
USER_SCHEMA = vol.Schema(
    {vol.Required(CONF_USERNAME): USERNAME_SELECTOR, vol.Required(CONF_PASSWORD): PASSWORD_SELECTOR}
)
REAUTH_SCHEMA = vol.Schema({vol.Required(CONF_PASSWORD): PASSWORD_SELECTOR})


class NoriaConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for a NOM account."""

    VERSION = 1
    MINOR_VERSION = 1

    async def _async_validate(self, username: str, password: str) -> dict[str, str]:
        """Check the credentials with a read-only device count."""
        try:
            # Raises ValueError for a username Basic auth can't carry (contains ":").
            client = NoriaClient(username, password, async_get_clientsession(self.hass))
            await client.async_get_device_count()
        except NoriaAuthError, NoriaForbiddenError, ValueError:
            return {"base": "invalid_auth"}
        except NoriaError:
            return {"base": "cannot_connect"}
        except Exception:
            LOGGER.exception("Unexpected error validating Noria credentials")
            return {"base": "unknown"}
        return {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask for the NOM account."""
        errors: dict[str, str] = {}
        if user_input is not None:
            username = user_input[CONF_USERNAME].strip()
            await self.async_set_unique_id(username.lower())
            self._abort_if_unique_id_configured()
            if not (errors := await self._async_validate(username, user_input[CONF_PASSWORD])):
                return self.async_create_entry(
                    title=username, data={CONF_USERNAME: username, CONF_PASSWORD: user_input[CONF_PASSWORD]}
                )
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                USER_SCHEMA, {CONF_USERNAME: user_input[CONF_USERNAME]} if user_input else None
            ),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """Start reauth when the stored password stops working."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask for the new password."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None and not (
            errors := await self._async_validate(entry.data[CONF_USERNAME], user_input[CONF_PASSWORD])
        ):
            return self.async_update_reload_and_abort(entry, data_updates={CONF_PASSWORD: user_input[CONF_PASSWORD]})
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=REAUTH_SCHEMA,
            description_placeholders={"username": entry.data[CONF_USERNAME]},
            errors=errors,
        )

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Change the credentials of the same account."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            username = user_input[CONF_USERNAME].strip()
            await self.async_set_unique_id(username.lower())
            self._abort_if_unique_id_mismatch(reason="wrong_account")
            if not (errors := await self._async_validate(username, user_input[CONF_PASSWORD])):
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_USERNAME: username, CONF_PASSWORD: user_input[CONF_PASSWORD]}
                )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(USER_SCHEMA, {CONF_USERNAME: entry.data[CONF_USERNAME]}),
            errors=errors,
        )
