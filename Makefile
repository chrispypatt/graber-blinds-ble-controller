CXX ?= g++
CXXFLAGS ?= -std=c++17 -Wall -Wextra
ESPHOME ?= esphome

.PHONY: test generate-example validate-example compile-example

test:
	python3 -m unittest discover -s tests -p 'test_*.py'
	$(CXX) $(CXXFLAGS) -Itests/stubs -Icomponents/graber_dispatcher \
		tests/test_dispatcher.cpp components/graber_dispatcher/graber_dispatcher.cpp \
		-o /tmp/graber_dispatcher_tests
	/tmp/graber_dispatcher_tests

generate-example:
	python3 tools/generate_controllers.py examples/controllers.example.json --output-dir .

validate-example: generate-example
	$(ESPHOME) config graber-example-controller.generated.yaml

compile-example: generate-example
	$(ESPHOME) compile graber-example-controller.generated.yaml --skip-bootloader
