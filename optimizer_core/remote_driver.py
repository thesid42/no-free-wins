"""Generic unprivileged I/O. This file is executed ONLY in the Linux sandbox."""

import importlib.util
import json
import sys


def validate(value):
    if type(value) in (int, str):
        return
    if type(value) is list:
        for item in value:
            validate(item)
        return
    if type(value) is dict and all(type(key) is str for key in value):
        for item in value.values():
            validate(item)
        return
    raise ValueError("lazy/custom/non-contract return type")


def main():
    specification = importlib.util.spec_from_file_location("candidate", sys.argv[1])
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    if not callable(getattr(module, "solve", None)):
        raise ValueError("candidate must define solve(data)")
    request = json.loads(sys.stdin.buffer.read())
    results = []
    for case in request["cases"]:
        value = module.solve(case["input"])
        validate(value)
        results.append({"id": case["id"], "output": value})
    sys.stdout.write(json.dumps({"results": results}, sort_keys=True,
                               separators=(",", ":"), ensure_ascii=False,
                               allow_nan=False))
    sys.stdout.flush()


if __name__ == "__main__":
    main()
