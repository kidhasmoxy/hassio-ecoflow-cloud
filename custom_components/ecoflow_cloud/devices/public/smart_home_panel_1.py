from custom_components.ecoflow_cloud.select import DictSelectEntity
from custom_components.ecoflow_cloud.switch import EnabledEntity
from custom_components.ecoflow_cloud.button import EnabledButtonEntity

import jsonpath_ng.ext as jp
from datetime import datetime

from ...api import EcoflowApiClient
from homeassistant.components.number import NumberEntity
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.restore_state import RestoreEntity
from ...entities import (
    EcoFlowAbstractEntity,
    BaseNumberEntity,
    BaseSelectEntity,
    BaseSensorEntity,
    BaseSwitchEntity,
)
from ...number import MaxBatteryLevelEntity, MinBatteryLevelEntity
from ...sensor import (
    LevelSensorEntity,
    OutWattsSensorEntity,
    WattsSensorEntity,
    MiscBinarySensorEntity,
    QuotaStatusSensorEntity,
    QuotaScheduledStatusSensorEntity,
)
from .. import BaseDevice, const

# SHP MQTT command constants (cmdSet 11)
CMD_SET_SHP = 11
CMD_ID_RTC_UPDATE = 3
CMD_ID_CIRCUIT_CTRL = 16
CMD_ID_GRID_CHARGE = 17
CMD_ID_EPS_MODE = 24
CMD_ID_LIMITS = 29
CMD_ID_CHANNEL_ENABLE = 26
CMD_ID_SELF_CHECK = 112

# System command set
CMD_SET_SYS = 1
CMD_ID_RESET = 20


class CircuitModeSelectEntity(DictSelectEntity):
    """Select for Circuit Mode using ctrlMode/ctrlSta combo.

    - Auto: ctrlMode=0, sta=0
    - Grid: ctrlMode=1, sta=0
    - Battery: ctrlMode=1, sta=1
    - Off: ctrlMode=1, sta=2
    """

    def __init__(
        self,
        client: EcoflowApiClient,
        device: BaseDevice,
        ch_index: int,
    ) -> None:
        self._ch = ch_index
        key = f"'heartbeat.loadCmdChCtrlInfos'[{ch_index}].ctrlSta"
        title = f"Circuit {ch_index + 1} Mode"
        options = {"Auto": 0, "Grid": 0, "Battery": 1, "Off": 2}
        super().__init__(client, device, key, title, options, command=None)

        # jsonpath for ctrlMode
        self._ctrl_mode_expr = jp.parse(
            self._adopt_json_key(f"'heartbeat.loadCmdChCtrlInfos'[{ch_index}].ctrlMode")
        )

    def _update_value(self, val):
        # Compute option based on ctrlMode and ctrlSta
        try:
            params = self._device.data.params
            mode_vals = self._ctrl_mode_expr.find(params)
            ctrl_mode = mode_vals[0].value if len(mode_vals) == 1 else None
            if ctrl_mode == 0:
                self._current_option = "Auto"
                return True
            elif ctrl_mode == 1:
                mapping = {0: "Grid", 1: "Battery", 2: "Off"}
                self._current_option = mapping.get(int(val))
                return self._current_option is not None
            else:
                return False
        except Exception:
            return False

    def select_option(self, option: str) -> None:
        # Build TCP command payload per YAML behavior
        if option == "Auto":
            sta = 0
            ctrl_mode = 0
            target = 0
        elif option == "Grid":
            sta = 0
            ctrl_mode = 1
            target = 0
        elif option == "Battery":
            sta = 1
            ctrl_mode = 1
            target = 1
        elif option == "Off":
            sta = 2
            ctrl_mode = 1
            target = 2
        else:
            return

        command = {
            "moduleType": 0,
            "operateType": "TCP",
            "params": {
                "sta": sta,
                "ctrlMode": ctrl_mode,
                "ch": self._ch,
                "cmdSet": CMD_SET_SHP,
                "id": CMD_ID_CIRCUIT_CTRL,
            },
        }

        # Update state and publish
        self.send_set_message(target, command)


class AggregatedWattsSensorEntity(WattsSensorEntity):
    """Aggregated watts sensor that also supports energy integration via with_energy()."""

    def __init__(self, client: EcoflowApiClient, device: BaseDevice, title: str, aggregator, unique_key: str = "infoList"):
        # Bind to infoList so we recompute on each heartbeat update; use unique_key for unique_id
        super().__init__(client, device, unique_key, title, enabled=True, auto_enable=True)
        self._aggregator = aggregator

    def _updated(self, data: dict):  # type: ignore[override]
        info = data.get("infoList")
        if isinstance(info, list) and len(info) >= 1:
            self._attr_available = True
            try:
                value = int(self._aggregator(info))
            except Exception:
                return
            if self._update_value(value):
                self.schedule_update_ha_state()


class SmartHomePanel1(BaseDevice):
    DEFAULT_SCHEDULED_RELOAD_SEC = 300

    def sensors(self, client: EcoflowApiClient) -> list[BaseSensorEntity]:
        sensors: list[BaseSensorEntity] = []

        # Circuit power (10 circuits)
        for i in range(10):
            # infoList[0..9].chWatt
            sensors.append(
                OutWattsSensorEntity(
                    client,
                    self,
                    f"'infoList'[{i}].chWatt",
                    const.BREAKER_N_POWER % (i + 1),
                )
                .with_energy()
                .attr(
                    f"'infoList'[{i}].powType",
                    "Power Source (0=Grid,1=Battery)",
                    0,
                )
                .attr(
                    f"'heartbeat.loadCmdChCtrlInfos'[{i}].ctrlSta",
                    "Circuit State",
                    0,
                )
                .attr(
                    f"'heartbeat.loadCmdChCtrlInfos'[{i}].ctrlMode",
                    "Control Mode (0=Auto,1=Manual)",
                    0,
                )
                .attr(
                    f"'heartbeat.loadCmdChCtrlInfos'[{i}].priority",
                    "Priority",
                    0,
                )
            )

        # Per-circuit source-specific power sensors with energy
        for i in range(10):
            sensors.append(
                AggregatedWattsSensorEntity(
                    client,
                    self,
                    f"Breaker {i + 1} Battery Power",
                    lambda info, idx=i: (
                        float(info[idx].get("chWatt", 0))
                        if (idx < len(info) and int(info[idx].get("powType", 0)) == 1)
                        else 0.0
                    ),
                    unique_key=f"infoList.breaker{ i + 1 }.battery",
                ).with_energy()
            )
            sensors.append(
                AggregatedWattsSensorEntity(
                    client,
                    self,
                    f"Breaker {i + 1} Grid Power",
                    lambda info, idx=i: (
                        float(info[idx].get("chWatt", 0))
                        if (idx < len(info) and int(info[idx].get("powType", 0)) == 0)
                        else 0.0
                    ),
                    unique_key=f"infoList.breaker{ i + 1 }.grid",
                ).with_energy()
            )

        # Battery power entries often appear at indices 10 and 11 in infoList
        # Map these as Battery 1/2 Power
        sensors.append(
            WattsSensorEntity(
                client,
                self,
                "'infoList'[10].chWatt",
                const.BATTERY_N_POWER % 1,
            ).with_energy().attr("'infoList'[10].powType", "Power Source (0=Grid,1=Battery)", 0)
        )
        sensors.append(
            WattsSensorEntity(
                client,
                self,
                "'infoList'[11].chWatt",
                const.BATTERY_N_POWER % 2,
            ).with_energy().attr("'infoList'[11].powType", "Power Source (0=Grid,1=Battery)", 0)
        )

        # Grid availability (0/1)
        sensors.append(
            MiscBinarySensorEntity(
                client,
                self,
                "'heartbeat.gridSta'",
                "Grid Available",
            )
        )

        # Battery percentage per AC1/AC2
        # Combined battery percentage
        sensors.append(
            LevelSensorEntity(
                client,
                self,
                "'heartbeat.backupBatPer'",
                const.COMBINED_BATTERY_LEVEL,
            )
        )

        # Individual batteries (AC1/AC2)
        sensors.append(
            LevelSensorEntity(
                client,
                self,
                "'heartbeat.energyInfos'[0].batteryPercentage",
                const.BATTERY_N_LEVEL % 1,
            )
            .attr("'heartbeat.energyInfos'[0].stateBean.isConnect", "Connected", 0)
            .attr("'heartbeat.energyInfos'[0].stateBean.isEnable", "Enabled", 0)
            .attr("'heartbeat.energyInfos'[0].stateBean.isGridCharge", "Grid Charging", 0)
            .attr("'heartbeat.energyInfos'[0].dischargeTime", "Discharge Time (min)", 0)
            .attr("'heartbeat.energyInfos'[0].chargeTime", "Charge Time (min)", 0)
            .attr("'heartbeat.energyInfos'[0].outputPower", "Output Power (W)", 0)
        )
        sensors.append(
            LevelSensorEntity(
                client,
                self,
                "'heartbeat.energyInfos'[1].batteryPercentage",
                const.BATTERY_N_LEVEL % 2,
            )
            .attr("'heartbeat.energyInfos'[1].stateBean.isConnect", "Connected", 0)
            .attr("'heartbeat.energyInfos'[1].stateBean.isEnable", "Enabled", 0)
            .attr("'heartbeat.energyInfos'[1].stateBean.isGridCharge", "Grid Charging", 0)
            .attr("'heartbeat.energyInfos'[1].dischargeTime", "Discharge Time (min)", 0)
            .attr("'heartbeat.energyInfos'[1].chargeTime", "Charge Time (min)", 0)
            .attr("'heartbeat.energyInfos'[1].outputPower", "Output Power (W)", 0)
        )

        # Aggregate sensors
        sensors.append(
            AggregatedWattsSensorEntity(
                client,
                self,
                "Circuits Combined Power",
                lambda info: sum(float(item.get("chWatt", 0)) for item in info[:10]),
                unique_key="infoList.total_circuits",
            ).with_energy()
        )
        sensors.append(
            AggregatedWattsSensorEntity(
                client,
                self,
                "Circuits Battery Demand Power",
                lambda info: sum(
                    float(item.get("chWatt", 0))
                    for item in info[:10]
                    if int(item.get("powType", 0)) == 1
                ),
                unique_key="infoList.total_circuits_battery",
            ).with_energy()
        )
        sensors.append(
            AggregatedWattsSensorEntity(
                client,
                self,
                "Circuits Grid Demand Power",
                lambda info: sum(
                    float(item.get("chWatt", 0))
                    for item in info[:10]
                    if int(item.get("powType", 0)) == 0
                ),
                unique_key="infoList.total_circuits_grid",
            ).with_energy()
        )

        # Combined battery power (sum of Battery 1 and 2 entries, if present at indices 10 and 11)
        sensors.append(
            AggregatedWattsSensorEntity(
                client,
                self,
                "Battery Combined Power",
                lambda info: sum(
                    float(info[i].get("chWatt", 0)) for i in (10, 11) if i < len(info)
                ),
                unique_key="infoList.total_battery_combined",
            ).with_energy()
        )

        # Add diagnostic/status sensors to keep data fresh and maintain connection
        # - QuotaStatusSensorEntity triggers on-demand REST refresh if MQTT is idle
        # - SHP1QuotaScheduledStatusSensorEntity forces a periodic refresh (interval configurable)
        sensors.append(QuotaStatusSensorEntity(client, self))
        sensors.append(SHP1QuotaScheduledStatusSensorEntity(client, self))

        return sensors

    def numbers(self, client: EcoflowApiClient) -> list[BaseNumberEntity]:
        numbers: list[BaseNumberEntity] = []

        # Charge limit (forceChargeHigh) 50..100%
        numbers.append(
            MaxBatteryLevelEntity(
                client,
                self,
                "'backupChaDiscCfg.forceChargeHigh'",
                const.MAX_CHARGE_LEVEL,
                50,
                100,
                lambda value, params: {
                    "moduleType": 0,
                    "operateType": "TCP",
                    "params": {
                        "discLower": int(params.get("backupChaDiscCfg.discLower", 0)),
                        "forceChargeHigh": int(value),
                        "cmdSet": CMD_SET_SHP,
                        "id": CMD_ID_LIMITS,
                    },
                },
            )
        )

        # Discharge limit (discLower) 0..30%
        numbers.append(
            MinBatteryLevelEntity(
                client,
                self,
                "'backupChaDiscCfg.discLower'",
                const.MIN_DISCHARGE_LEVEL,
                0,
                30,
                lambda value, params: {
                    "moduleType": 0,
                    "operateType": "TCP",
                    "params": {
                        "discLower": int(value),
                        "forceChargeHigh": int(params.get("backupChaDiscCfg.forceChargeHigh", 100)),
                        "cmdSet": CMD_SET_SHP,
                        "id": CMD_ID_LIMITS,
                    },
                },
            )
        )

        # Configuration: Scheduled refresh interval
        numbers.append(ScheduledRefreshIntervalNumber(client, self))

        return numbers

    def switches(self, client: EcoflowApiClient) -> list[BaseSwitchEntity]:
        switches: list[BaseSwitchEntity] = []

        # EPS Mode switch (state is reported under 'epsModeInfo.eps')
        switches.append(
            EnabledEntity(
                client,
                self,
                "'epsModeInfo.eps'",
                const.EPS_MODE,
                lambda value: {
                    "moduleType": 0,
                    "operateType": "TCP",
                    "params": {
                        "cmdSet": CMD_SET_SHP,
                        "id": CMD_ID_EPS_MODE,
                        "eps": 1 if int(value) == 1 else 0,
                    },
                },
            )
        )

        # AC1 Recharge (uses ch 10, id 17). Tie state to heartbeat energyInfos[0].stateBean.isGridCharge
        switches.append(
            EnabledEntity(
                client,
                self,
                "'heartbeat.energyInfos'[0].stateBean.isGridCharge",
                "AC1 Grid Charging",
                lambda value: {
                    "moduleType": 0,
                    "operateType": "TCP",
                    "params": {
                        "sta": 2 if int(value) == 1 else 0,
                        "ctrlMode": 1,
                        "ch": 10,
                        "cmdSet": CMD_SET_SHP,
                        "id": CMD_ID_GRID_CHARGE,
                    },
                },
            )
        )

        # AC2 Recharge (uses ch 11, id 17). Tie state to heartbeat energyInfos[1].stateBean.isGridCharge
        switches.append(
            EnabledEntity(
                client,
                self,
                "'heartbeat.energyInfos'[1].stateBean.isGridCharge",
                "AC2 Grid Charging",
                lambda value: {
                    "moduleType": 0,
                    "operateType": "TCP",
                    "params": {
                        "sta": 2 if int(value) == 1 else 0,
                        "ctrlMode": 1,
                        "ch": 11,
                        "cmdSet": CMD_SET_SHP,
                        "id": CMD_ID_GRID_CHARGE,
                    },
                },
            )
        )

        # Note: Removed per-circuit enable/disable switches from controls by request

        return switches
 
    def buttons(self, client: EcoflowApiClient):
        # Update Real-Time Clock button (id 3)
        return [
            EnabledButtonEntity(
                client,
                self,
                "'rtcUpdate'",
                "Update Real-Time Clock",
                lambda value: {
                    "moduleType": 0,
                    "operateType": "TCP",
                    "params": {
                        "cmdSet": CMD_SET_SHP,
                        "id": CMD_ID_RTC_UPDATE,
                        # ISO weekday: Monday=1 .. Sunday=7
                        "week": datetime.now().isoweekday(),
                        "sec": datetime.now().second,
                        "min": datetime.now().minute,
                        "hour": datetime.now().hour,
                        "day": datetime.now().day,
                        "month": datetime.now().month,
                        "year": datetime.now().year,
                    },
                },
                enabled=False,
            ),
            EnabledButtonEntity(
                client,
                self,
                "'selfCheck'",
                "Start Self-Check",
                lambda value: {
                    "moduleType": 0,
                    "operateType": "TCP",
                    "params": {
                        "cmdSet": CMD_SET_SHP,
                        "id": CMD_ID_SELF_CHECK,
                        "selfCheckType": 1,
                    },
                },
                enabled=False,
            ),
            EnabledButtonEntity(
                client,
                self,
                "'reset'",
                "Reset",
                lambda value: {
                    "moduleType": 0,
                    "operateType": "TCP",
                    "params": {
                        "cmdSet": CMD_SET_SYS,
                        "id": CMD_ID_RESET,
                    },
                },
                enabled=False,
            ),
        ]

    def selects(self, client: EcoflowApiClient) -> list[BaseSelectEntity]:
        selects: list[BaseSelectEntity] = []

        # Per-circuit mode: Auto/Grid/Battery/Off
        # Auto is represented by ctrlMode=0; Manual modes use ctrlMode=1 with sta=0/1/2
        for i in range(10):
            selects.append(CircuitModeSelectEntity(client, self, i))

        return selects

    def flat_json(self):
        # SHP payloads are nested; use the same flattening approach as SHP2
        return False

    def _prepare_data(self, raw_data) -> dict[str, any]:
        # Mirror SHP2 data preparation: merge param/params and flatten dicts one level
        res = super()._prepare_data(raw_data)
        new_params: dict[str, any] = {}

        # Merge nested content from 'param' and 'params' if present
        if "param" in res and isinstance(res["param"], dict):
            for k, v in res["param"].items():
                new_params[k] = v
        if "params" in res and isinstance(res["params"], dict):
            for k, v in res["params"].items():
                new_params[k] = v

        # Optionally include other top-level keys (excluding the containers) for completeness
        for k, v in res.items():
            if k not in ("param", "params"):
                new_params[k] = v

        # Shallow-flatten dicts one level: produce keys like 'heartbeat.gridSta'
        new_params2: dict[str, any] = {}
        for k, v in new_params.items():
            new_params2[k] = v
            if isinstance(v, dict):
                for k2, v2 in v.items():
                    new_params2[f"{k}.{k2}"] = v2

        return {"params": new_params2, "raw_data": res}


# Device-local configuration entity: sets scheduled quota refresh interval (seconds)
class ScheduledRefreshIntervalNumber(NumberEntity, EcoFlowAbstractEntity, RestoreEntity):
    _attr_entity_category = EntityCategory.CONFIG
    _attr_native_min_value = 60
    _attr_native_max_value = 3600
    _attr_native_step = 30
    _attr_has_entity_name = True

    def __init__(self, client: EcoflowApiClient, device: BaseDevice):
        # Use unique key under the device for config
        EcoFlowAbstractEntity.__init__(
            self, client, device, "Status Scheduled Refresh Interval", "status.scheduled_interval"
        )
        # Initialize device property if missing
        if not hasattr(self._device, "_shp1_reload_delay"):
            setattr(self._device, "_shp1_reload_delay", SmartHomePanel1.DEFAULT_SCHEDULED_RELOAD_SEC)
        self._attr_native_value = int(getattr(self._device, "_shp1_reload_delay"))

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state and last_state.state not in ("unknown", "unavailable"):
            try:
                restored = int(float(last_state.state))
                setattr(self._device, "_shp1_reload_delay", restored)
                self._attr_native_value = restored
            except Exception:
                pass

    async def async_set_native_value(self, value: float) -> None:
        # Persist on the device instance so scheduled status sensor can pick it up
        setattr(self._device, "_shp1_reload_delay", int(value))
        self._attr_native_value = int(value)
        self.schedule_update_ha_state()

    def _handle_coordinator_update(self) -> None:
        # No-op: value is purely a local config; but keep UI in sync if changed elsewhere
        current = int(getattr(self._device, "_shp1_reload_delay", SmartHomePanel1.DEFAULT_SCHEDULED_RELOAD_SEC))
        if self._attr_native_value != current:
            self._attr_native_value = current
            self.schedule_update_ha_state()


# Scheduled status sensor that reads interval dynamically from the device property
class SHP1QuotaScheduledStatusSensorEntity(QuotaScheduledStatusSensorEntity):
    def __init__(self, client: EcoflowApiClient, device: BaseDevice):
        reload_delay = int(
            getattr(device, "_shp1_reload_delay", SmartHomePanel1.DEFAULT_SCHEDULED_RELOAD_SEC)
        )
        super().__init__(client, device, reload_delay=reload_delay)

    def _actualize_status(self) -> bool:
        # Update the interval dynamically before evaluating the schedule
        self.offline_barrier_sec = int(
            getattr(self._device, "_shp1_reload_delay", SmartHomePanel1.DEFAULT_SCHEDULED_RELOAD_SEC)
        )
        return super()._actualize_status()
