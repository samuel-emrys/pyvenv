# CMakePythonEnvironment Generator

A Conan generator to provide CMake targets for executables installed within python virtual environments.

## Usage


For a recipe to be consumed using the `CMakePythonEnvironment` generator, populate the `self.user_info.python_requirements` and `self.user_info.python_env` `package_info()` variables as below:

```python
class PythonVirtualEnvironment(ConanFile):
    options = {"requirements": "ANY"}
    default_options = {"requirements": "[]"}

    def package_info(self):
        self.user_info.python_requirements = self.options.get_safe("requirements", "[]")
        self.user_info.python_envdir = self.package_folder
```

* `user_info.python_requirements`: This is expected to be a JSON string containing a list of the packages and their versions to be installed into the virtual environment. An example of this might look like:

```python
requirements = [
    "sphinx==4.4.0",
    "sphinx_rtd_theme",
    "sphinx_book_theme==0.3.2",
    "pygments",
]

self.options.requirements = json.dumps(requirements)
```

An example of a package that uses this is the [`python-virtualenv/system`](https://github.com/samuel-emrys/python-virtualenv) package.

To consume a recipe that has populated the above variables, simply specify `CMakePythonEnvironment` as the generator to use in the consumer `conanfile.py`:

```python
class ExamplePythonConan(ConanFile):
    # ...
    generators = "CMakePythonEnvironment"
```

An example of this being used in conjunction with `python-virtualenv` is [`sphinx-consumer`](https://github.com/samuel-emrys/sphinx-consumer).


This has been developed with significant inspiration from, and uses `pyvenv` code largely developed by [thorntonryan/conan-pyvenv](https://github.com/thorntonryan/conan-pyvenv).
