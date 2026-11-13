import esphome.codegen as cg
import esphome.config_validation as cv
from esphome.components import ble_client, sensor, text_sensor
from esphome.const import CONF_ID

DEPENDENCIES = ["ble_client"]
AUTO_LOAD = ["sensor", "text_sensor"]
MULTI_CONF = True
ns = cg.esphome_ns.namespace("graber_dispatcher")
GraberDispatcher = ns.class_("GraberDispatcher", cg.Component, ble_client.BLEClientNode)

def _validate_rails(config):
    if config["dual_rail"]:
        for key in ("top_sensor", "top_notify_sensor"):
            if key not in config:
                raise cv.Invalid(f"{key} is required when dual_rail is true", path=[key])
    return config


CONFIG_SCHEMA = cv.All(cv.Schema({
    cv.GenerateID(): cv.declare_id(GraberDispatcher),
    cv.Required("token"): cv.int_range(min=0, max=9999999),
    cv.Required("label"): cv.string,
    cv.Optional("dual_rail", default=True): cv.boolean,
    cv.Required("bottom_sensor"): cv.use_id(sensor.Sensor),
    cv.Optional("top_sensor"): cv.use_id(sensor.Sensor),
    cv.Required("bottom_notify_sensor"): cv.use_id(sensor.Sensor),
    cv.Optional("top_notify_sensor"): cv.use_id(sensor.Sensor),
    cv.Required("battery_sensor"): cv.use_id(sensor.Sensor),
    cv.Required("status_sensor"): cv.use_id(text_sensor.TextSensor),
}).extend(ble_client.BLE_CLIENT_SCHEMA).extend(cv.COMPONENT_SCHEMA), _validate_rails)

async def to_code(config):
    var = cg.new_Pvariable(config[CONF_ID])
    await cg.register_component(var, config)
    await ble_client.register_ble_node(var, config)
    cg.add(var.set_token(config["token"]))
    cg.add(var.set_label(config["label"]))
    cg.add(var.set_dual_rail(config["dual_rail"]))
    for key in ("bottom_sensor", "top_sensor", "bottom_notify_sensor", "top_notify_sensor", "battery_sensor", "status_sensor"):
        if key in config:
            cg.add(getattr(var, "set_" + key)(await cg.get_variable(config[key])))
