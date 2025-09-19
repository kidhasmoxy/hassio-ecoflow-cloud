from custom_components.ecoflow_cloud.select import DictSelectEntity
from custom_components.ecoflow_cloud.switch import EnabledEntity
from custom_components.ecoflow_cloud.button import EnabledButtonEntity

import jsonpath_ng.ext as jp
from datetime import datetime
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import UnitOfPower

from ...api import EcoflowApiClient
from ...entities import (
    BaseNumberEntity,
    BaseSelectEntity,
    BaseSensorEntity,
    BaseSwitchEntity,
)
from ...number import ChargingPowerEntity, MaxBatteryLevelEntity, MinBatteryLevelEntity
from ...sensor import (
    InWattsSensorEntity,
    LevelSensorEntity,
    OutWattsSensorEntity,
    RemainSensorEntity,
    WattsSensorEntity,
    MiscSensorEntity,
    MiscBinarySensorEntity,
)
from .. import BaseDevice, const


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
                "cmdSet": 11,
                "id": 16,
            },
        }

        # Update state and publish
        self.send_set_message(target, command)


class AggregatedWattsSensorEntity(WattsSensorEntity):
    """Aggregated watts sensor that also supports energy integration via with_energy()."""

    def __init__(self, client: EcoflowApiClient, device: BaseDevice, title: str, aggregator):
        # Bind to infoList so we recompute on each heartbeat update
        super().__init__(client, device, "infoList", title, enabled=True, auto_enable=True)
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
            ).with_energy()
        )

        return sensors

    def numbers(self, client: EcoflowApiClient) -> list[BaseNumberEntity]:
        numbers: list[BaseNumberEntity] = []

        # Charge limit (forceChargeHigh) 50..100%
        numbers.append(
            MaxBatteryLevelEntity(
                client,
                self,
                "'limits.forceChargeHigh'",
                const.MAX_CHARGE_LEVEL,
                50,
                100,
                lambda value, params: {
                    "moduleType": 0,
                    "operateType": "TCP",
                    "params": {
                        "discLower": int(params.get("limits.discLower", 0)),
                        "forceChargeHigh": int(value),
                        "cmdSet": 11,
                        "id": 29,
                    },
                },
            )
        )

        # Discharge limit (discLower) 0..30%
        numbers.append(
            MinBatteryLevelEntity(
                client,
                self,
                "'limits.discLower'",
                const.MIN_DISCHARGE_LEVEL,
                0,
                30,
                lambda value, params: {
                    "moduleType": 0,
                    "operateType": "TCP",
                    "params": {
                        "discLower": int(value),
                        "forceChargeHigh": int(params.get("limits.forceChargeHigh", 100)),
                        "cmdSet": 11,
                        "id": 29,
                    },
                },
            )
        )

        return numbers

    def switches(self, client: EcoflowApiClient) -> list[BaseSwitchEntity]:
        switches: list[BaseSwitchEntity] = []

        # EPS Mode switch (no direct heartbeat key; store state under 'epsState')
        switches.append(
            EnabledEntity(
                client,
                self,
                "'epsState'",
                const.EPS_MODE,
                lambda value: {
                    "moduleType": 0,
                    "operateType": "TCP",
                    "params": {"cmdSet": 11, "id": 24, "eps": 1 if int(value) == 1 else 0},
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
                        "ctrlMode": 1 if int(value) == 1 else 0,
                        "ch": 10,
                        "cmdSet": 11,
                        "id": 17,
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
                        "ctrlMode": 1 if int(value) == 1 else 0,
                        "ch": 11,
                        "cmdSet": 11,
                        "id": 17,
                    },
                },
            )
        )

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
                        "cmdSet": 11,
                        "id": 3,
                        "week": int(datetime.now().strftime("%U")),
                        "sec": datetime.now().second,
                        "min": datetime.now().minute,
                        "hour": datetime.now().hour,
                        "day": datetime.now().day,
                        "month": datetime.now().month,
                        "year": datetime.now().year,
                    },
                },
            )
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
