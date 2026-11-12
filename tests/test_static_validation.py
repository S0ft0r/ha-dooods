"""Project-level safety checks that protect against known review regressions."""

from __future__ import annotations

import json
import struct
import unittest
from pathlib import Path

from scripts.validate import validate


class StaticValidationTests(unittest.TestCase):
    def test_domres_source_has_no_known_static_review_errors(self) -> None:
        errors, _warnings = validate()
        self.assertEqual(errors, [])

    def test_retired_indication_binary_sensor_is_not_forwarded_and_is_cleaned_up(self) -> None:
        component = Path(__file__).resolve().parents[1] / "custom_components/domres"
        setup_source = (component / "__init__.py").read_text(encoding="utf-8")
        self.assertIn(
            "PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.NUMBER, Platform.BUTTON]",
            setup_source,
        )
        self.assertIn("async_entries_for_config_entry", setup_source)
        self.assertIn("registry.async_remove", setup_source)
        self.assertFalse((component / "binary_sensor.py").exists())

    def test_last_readings_sensor_uses_requested_short_friendly_name(self) -> None:
        component = Path(__file__).resolve().parents[1] / "custom_components/domres"
        sensor_source = (component / "sensor.py").read_text(encoding="utf-8")
        last_readings_class = sensor_source.split("class DomResLastReadingSensor", 1)[1].split(
            "\n\nasync def async_setup_entry", 1
        )[0]
        self.assertIn("_attr_has_entity_name = False", last_readings_class)
        self.assertIn('name = "Последние показания"', last_readings_class)
        self.assertIn("def native_value(self) -> int | None", last_readings_class)
        self.assertIn("integer_reading(scale.get(\"previous_reading\"))", last_readings_class)
        self.assertNotIn("meter['name']", last_readings_class)

    def test_sensor_platform_imports_domain_for_platform_setup(self) -> None:
        component = Path(__file__).resolve().parents[1] / "custom_components/domres"
        sensor_source = (component / "sensor.py").read_text(encoding="utf-8")
        self.assertIn("from .const import DOMAIN", sensor_source)

    def test_legacy_reading_names_are_migrated_and_custom_names_preserved(self) -> None:
        component = Path(__file__).resolve().parents[1] / "custom_components/domres"
        setup_source = (component / "__init__.py").read_text(encoding="utf-8")
        self.assertIn("_shorten_last_reading_names(hass, entry)", setup_source)
        self.assertIn('"_reading_" not in entity.unique_id', setup_source)
        self.assertIn('entity.name.startswith(old_prefixes)', setup_source)
        self.assertIn('name="Последние показания"', setup_source)

    def test_reconfigure_action_is_implemented_and_translated(self) -> None:
        component = Path(__file__).resolve().parents[1] / "custom_components/domres"
        flow_source = (component / "config_flow.py").read_text(encoding="utf-8")
        self.assertIn("async def async_step_reconfigure", flow_source)
        self.assertIn("async_update_reload_and_abort", flow_source)
        self.assertIn("_abort_if_unique_id_mismatch(reason=\"wrong_account\")", flow_source)
        for language in ("ru", "en", "strings"):
            translation = json.loads(
                (component / "translations" / f"{language}.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertIn("reconfigure", translation["config"]["step"])
            self.assertIn("wrong_account", translation["config"]["abort"])
            self.assertNotIn("initiate_flow", translation["config"])

    def test_balance_labels_and_requested_icons(self) -> None:
        component = Path(__file__).resolve().parents[1] / "custom_components/domres"
        sensor_source = (component / "sensor.py").read_text(encoding="utf-8")
        button_source = (component / "button.py").read_text(encoding="utf-8")
        debt_source = sensor_source.split("class DomResDebtSensor", 1)[1].split(
            "class DomResFineSensor", 1
        )[0]
        fine_source = sensor_source.split("class DomResFineSensor", 1)[1].split(
            "class DomResDaysSensor", 1
        )[0]
        self.assertIn('_attr_icon = "mdi:cash-plus"', debt_source)
        self.assertIn('"debt", "К оплате"', debt_source)
        self.assertIn('_attr_icon = "mdi:cash-minus"', fine_source)
        self.assertIn('_attr_icon = "mdi:invoice-text-send-outline"', button_source)

    def test_domres_event_blueprint_notifies_and_gates_submission(self) -> None:
        root = Path(__file__).resolve().parents[1]
        blueprint = (root / "blueprint/domres.yaml").read_text(
            encoding="utf-8"
        )
        self.assertIn("domres_reading_deadline", blueprint)
        self.assertIn("domres_unpaid_bill", blueprint)
        self.assertIn("!input notify_service", blueprint)
        self.assertIn("service: notify.send_message", blueprint)
        self.assertNotIn("service: !input notify_service", blueprint)
        self.assertIn("number.set_value", blueprint)
        self.assertIn("button.press", blueprint)
        self.assertIn("submission_enabled", blueprint)
        self.assertIn("minimum_allowed", blueprint)

    def test_brand_icons_use_home_assistant_png_sizes(self) -> None:
        component = Path(__file__).resolve().parents[1] / "custom_components/domres"
        dimensions = {
            "icon.png": (256, 256),
            "dark_icon.png": (256, 256),
            "icon@2x.png": (512, 512),
            "dark_icon@2x.png": (512, 512),
        }
        for filename, expected_size in dimensions.items():
            image = (component / "brand" / filename).read_bytes()
            self.assertEqual(image[:8], b"\x89PNG\r\n\x1a\n")
            self.assertEqual(struct.unpack(">II", image[16:24]), expected_size)

    def test_reading_input_button_is_visible_but_never_sends_network_requests(self) -> None:
        component = Path(__file__).resolve().parents[1] / "custom_components/domres"
        setup_source = (component / "__init__.py").read_text(encoding="utf-8")
        button_source = (component / "button.py").read_text(encoding="utf-8")
        number_source = (component / "number.py").read_text(encoding="utf-8")
        self.assertIn("Platform.NUMBER", setup_source)
        self.assertIn("Platform.BUTTON", setup_source)
        self.assertIn("async def async_press", button_source)
        self.assertIn("HomeAssistantError", button_source)
        self.assertIn("reading_not_below_previous", button_source)
        self.assertIn("def native_min_value", number_source)
        self.assertIn("reading_not_below_previous", number_source)
        self.assertIn("_attr_native_step = 1", number_source)
        self.assertIn("_attr_suggested_display_precision = 0", number_source)
        self.assertIn("integer_reading(value)", number_source)
        self.assertIn("def native_value(self) -> int", number_source)
        self.assertIn('self._attr_name = "Показание"', number_source)
        self.assertIn('self._attr_name = "Отправить"', button_source)
        self.assertIn('"account_id": self._account_id', number_source)
        self.assertIn('"minimum_allowed": self.native_min_value', number_source)
        self.assertIn('"submission_enabled": False', button_source)
        self.assertIn('"account_id": self._account_id', button_source)
        self.assertNotIn("coordinator.api", button_source)
        self.assertNotIn("_async_request", button_source)

    def test_deadline_event_uses_configured_threshold(self) -> None:
        component = Path(__file__).resolve().parents[1] / "custom_components/domres"
        flow_source = (component / "config_flow.py").read_text(encoding="utf-8")
        coordinator_source = (component / "coordinator.py").read_text(encoding="utf-8")
        self.assertIn("CONF_DEADLINE_DAYS", flow_source)
        self.assertIn("deadline_days", coordinator_source)
        self.assertIn("async_fire", coordinator_source)

    def test_unpaid_event_is_keyed_by_status_period_and_persisted(self) -> None:
        component = Path(__file__).resolve().parents[1] / "custom_components/domres"
        api_source = (component / "api.py").read_text(encoding="utf-8")
        event_source = (component / "events.py").read_text(encoding="utf-8")
        coordinator_source = (component / "coordinator.py").read_text(encoding="utf-8")
        self.assertIn("/indications/status", api_source)
        self.assertIn("datePeriodFrom", event_source)
        self.assertIn("datePeriodTo", event_source)
        self.assertIn("async_save", coordinator_source)


if __name__ == "__main__":
    unittest.main()
