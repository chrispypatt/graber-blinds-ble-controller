import copy
import importlib.util
import json
import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "generate_controllers", ROOT / "tools" / "generate_controllers.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def example():
    return json.loads((ROOT / "examples" / "controllers.example.json").read_text())


class GeneratorTests(unittest.TestCase):
    def test_example_validates_and_quotes_names(self):
        config = example()
        config["controllers"][0]["friendly_name"] = "Kitchen: West #1"
        config["controllers"][0]["blinds"][0]["name"] = "Left: #1"
        MODULE.validate(config)
        rendered = MODULE.render(config["controllers"][0])
        self.assertIn('friendly_name: "Kitchen: West #1"', rendered)
        self.assertIn('blind_name: "Left: #1"', rendered)

    def test_rejects_output_path_traversal(self):
        config = example()
        config["controllers"][0]["file"] = "../secrets.yaml"
        with self.assertRaises(ValueError):
            MODULE.validate(config)

    def test_rejects_non_boolean_dual_rail(self):
        config = example()
        config["controllers"][0]["blinds"][0]["dual_rail"] = "yes"
        with self.assertRaises(ValueError):
            MODULE.validate(config)

    def test_rejects_duplicate_controller_output(self):
        config = example()
        duplicate = copy.deepcopy(config["controllers"][0])
        duplicate["name"] = "another-controller"
        config["controllers"].append(duplicate)
        with self.assertRaises(ValueError):
            MODULE.validate(config)

    def test_rejects_unknown_group_member(self):
        config = example()
        config["controllers"][0]["groups"][0]["blinds"].append("missing")
        with self.assertRaises(ValueError):
            MODULE.validate(config)

    def test_group_stack_actions_include_only_declared_tdbu_motors(self):
        config = example()
        MODULE.validate(config)
        rendered = MODULE.render(config["controllers"][0])
        self.assertIn('name: "Living Bulk Stack Up"', rendered)
        self.assertIn('name: "Living Bulk Stack Down"', rendered)
        self.assertIn('9, {id(living_left_dispatcher)}', rendered)
        self.assertIn('10, {id(living_left_dispatcher)}', rendered)
        self.assertNotIn('id(living_right_dispatcher)});', rendered.split('Living Bulk Stack Up')[-1])

    def test_group_rejects_single_rail_in_tdbu_members(self):
        config = example()
        config["controllers"][0]["groups"][0]["tdbu_blinds"] = ["living_right"]
        with self.assertRaises(ValueError):
            MODULE.validate(config)


if __name__ == "__main__":
    unittest.main()
