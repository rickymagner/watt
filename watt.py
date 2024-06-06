import argparse
import sys
import os
import subprocess
from ruamel.yaml import YAML
import json
import filecmp
import gzip
import multiprocess as mp
from dataclasses import dataclass
from typing import List, Dict
from pathlib import Path


try:
    width = os.get_terminal_size(0).columns - 10
except OSError:
    width = 60  # Default if above method fails

OUTPUT_SEPARATOR = width * '-'
CONFIG_DEFAULT_NAME = "watt_config.yml"

parser = argparse.ArgumentParser()
parser.add_argument("-w", "--workflow", help="Name of workflow(s) whose tests should be run.", nargs="+")
parser.add_argument("-t", "--test", help="Specific name of test to run; only tests matching this name will be run.",
                    nargs="+")
parser.add_argument("-e", "--executor", help="Path to cromwell jar.", default=os.environ.get("EXECUTION_ENGINE"))
parser.add_argument("--executor-log-prefix",
                    help="Prefix for cromwell log path; outputs will be [flag_input]-[workflow]-[test_name].log.",
                    default="watt_logs/cromwell")
parser.add_argument("-c", "--config", help="Test configuration file.", default=CONFIG_DEFAULT_NAME)
parser.add_argument("-l", "--log", help="Where to print test log after running. Only works with single process.",
                    type=argparse.FileType('w'), default=sys.stdout)
parser.add_argument("-p", "--processes", help="Number of processes to run tests concurrently.", type=int, default=1)


def resolve_relative_path(rel_path: str) -> str:
    """
    Given test path, check if it should be interpreted as a relative path inside a repo or absolute path on system.
    Return value is an absolute path on the host system pointing to the file at the given path.
    This should allow users running tests on local machines to resolve the correct paths, even if the repo has a
    root given by a non-root dir on the local system.
    """
    in_repo = False
    in_root = False
    working_dir = os.path.curdir
    while not in_repo and not in_root:
        if os.path.exists(os.path.join(working_dir, os.path.join('.git'))):
            in_repo = True
        else:
            new_working_dir = os.path.abspath(os.path.join(working_dir, '..'))
            in_root = working_dir == new_working_dir
            working_dir = new_working_dir
    if in_repo:
        return os.path.join(working_dir, rel_path.removeprefix('/'))
    else:
        return rel_path

@dataclass
class CromwellConfig:
    """
    A class to hold Cromwell runtime information.
    """
    jar_path: str
    log_prefix: str

    def run(self, wdl_path: str, input_json: str, output_json: str, log_path_str: str) -> int:
        """
        Calls shell to run Cromwell and returns the exit code.
        """
        # Run cromwell and return exit code
        cmd = ['java', '-jar', self.jar_path, 'run', wdl_path, '--inputs', input_json, '--metadata-output', output_json]
        log_path = Path(log_path_str)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open('w') as logfile:
            completed = subprocess.run(cmd, stdout=logfile, stderr=logfile)
        return completed.returncode


# Wanted to use Enum, but doesn't serialize well for multiprocessing and leads to various warnings polluting output
@dataclass
class ComparisonResult:
    """
    An enum-like dataclass of the possible outcomes of comparing two values of WDL outputs.
    """
    Match = 0  # Values make sense to compare and are equal.
    Mismatch = 1  # Values make sense to compare but are not equal.
    ArrayShapeMismatch = 2  # Values do not make sense to compare because they have different Array shapes.
    FileTypeMismatch = 3  # Values do not make sense to compare because exactly one appears to be a File.

    def get_possible_values(self):
        """
        Method to collect all possible states of the enum-like class.
        """
        return [self.Match, self.Mismatch, self.ArrayShapeMismatch, self.FileTypeMismatch]


@dataclass
class JsonComparisonResult:
    """
    A dataclass holding the results of comparing two JSON files.
    """
    unique_expected_keys: List[str]  # List of keys unique to the expected outputs JSON
    unique_actual_keys: List[str]  # List of keys unique to the actual outputs JSON
    key_statuses: Dict[str, int]  # Holds test results per key for overlapping key values

    def get_results_to_keys(self) -> Dict[int, List[str]]:
        """
        Takes the key_statuses dict and reverses it so that each ComparisonResult points to the list of keys having that result type.
        """
        reversed_dict = {}
        for key, value in self.key_statuses.items():
            reversed_dict.setdefault(value, [])
            reversed_dict[value].append(key)
        for r in ComparisonResult().get_possible_values():
            if r not in reversed_dict.keys():
                reversed_dict[r] = []
        return reversed_dict


@dataclass
class CompareOutputs:
    """
    A class for holding methods relating to comparing the outputs from a WDL run to an expected JSON.
    """

    def compare_jsons(self, expected_outputs: str, actual_outputs: str) -> JsonComparisonResult:
        """
        Performs the actual comparison between two WDL-output-like JSON files.
        """
        # Compares both JSONs and returns tuple of successes/failures listed by JSON key value

        with open(expected_outputs, 'r') as expected:
            expected_json = json.loads(expected.read())

        with open(actual_outputs, 'r') as actual:
            actual_json = json.loads(actual.read())['outputs']

        unique_expected_keys = []
        for k in expected_json:
            if k not in actual_json:
                unique_expected_keys += [k]

        unique_actual_keys = []
        for k in actual_json:
            if k not in expected_json:
                unique_actual_keys += [k]

        key_statuses = {}
        for k in expected_json:
            if k not in unique_expected_keys:
                if expected_json[k]:
                    key_statuses[k] = self.match(expected_json[k], actual_json[k])

        return JsonComparisonResult(unique_expected_keys=unique_expected_keys, unique_actual_keys=unique_actual_keys,
                                    key_statuses=key_statuses)

    def match(self, x, y) -> int:
        """
        Performs a comparison against two values from an output JSON. Uses recursion to handle nested Array types, and
        infers File types from attempting to read strings as file first. If that fails, then compare raw values.
        """
        # Check if they're both arrays
        if isinstance(x, list) and isinstance(y, list):
            if len(x) == len(y):
                # If same length, try to match all entries in each list
                # One ArrayShapeMismatch or FileTypeMismatch will spoil the whole comparison, with those priorities
                nested_values = [self.match(xi, yi) for xi, yi in zip(x, y)]
                if all([v == ComparisonResult.Match for v in nested_values]):
                    return ComparisonResult.Match
                elif any([v == ComparisonResult.ArrayShapeMismatch for v in nested_values]):
                    return ComparisonResult.ArrayShapeMismatch
                elif any([v == ComparisonResult.FileTypeMismatch for v in nested_values]):
                    return ComparisonResult.FileTypeMismatch
                else:
                    return ComparisonResult.Mismatch
            else:
                return ComparisonResult.ArrayShapeMismatch
        elif isinstance(x, list) or isinstance(y, list):
            # This means at some level of nesting one is Array and the other is not, so the tensors have different shapes
            return ComparisonResult.ArrayShapeMismatch
        else:
            # Attempt to resolve strings as paths, and if so compare file contents
            if isinstance(x, str) and isinstance(y, str):
                if os.path.exists(x) and os.path.exists(y):
                    try:
                        with gzip.open(x, 'r') as x_file, gzip.open(y, 'r') as y_file:
                            x_contents = x_file.read()
                            y_contents = y_file.read()
                            if x_contents == y_contents:
                                return ComparisonResult.Match
                            else:
                                return ComparisonResult.Mismatch
                    except gzip.BadGzipFile:
                        if filecmp.cmp(x, y, shallow=False):
                            return ComparisonResult.Match
                        else:
                            return ComparisonResult.Mismatch
                elif os.path.exists(x) or os.path.exists(y):
                    return ComparisonResult.FileTypeMismatch
                else:
                    return ComparisonResult.Match if x == y else ComparisonResult.Mismatch
            else:
                # If not file or array, just compare the raw values
                return ComparisonResult.Match if x == y else ComparisonResult.Mismatch


@dataclass
class TestResult:
    """
    A class for holding the data associated with running a single WDLTest.
    """
    status: int
    expect_fail: bool
    cromwell_fail: bool
    json_comparison: JsonComparisonResult or None


@dataclass
class WDLTest:
    """
    A class for holding the metadata and runtime information about performing a single WDL test.
    """
    workflow_name: str
    test_name: str
    path: str
    test_inputs: str
    expected_outputs: str or None
    cromwell_config: CromwellConfig

    def run_test(self) -> TestResult:
        """
        Creates the return TestResult object by running Cromwell, and comparing the actual outputs to the expected ones.
        If no expected JSON is provided, then assumed the user expects the run to fail.
        """
        # Create Cromwell subprocess with self parameters
        output_path = f'cromwell-executions/watt/result-{self.workflow_name}-{self.test_name}-outputs.json'
        stem_sep = '-' if self.cromwell_config.log_prefix[-1] != '/' else ''  # If stem is dir, start filename without '-'
        log_path = f'{self.cromwell_config.log_prefix}{stem_sep}{self.workflow_name}-{self.test_name}.log'
        cromwell_result = self.cromwell_config.run(self.path, self.test_inputs, output_path, log_path)

        if self.expected_outputs is None:
            # Test succeeds only if the run failed in this case
            status = 0 if cromwell_result > 0 else 1
            return TestResult(status=status, expect_fail=True, cromwell_fail=cromwell_result > 0, json_comparison=None)
        elif cromwell_result > 0:
            # Cromwell failed to run but expected outputs
            return TestResult(status=cromwell_result, expect_fail=False, cromwell_fail=True, json_comparison=None)
        else:
            # Cromwell ran successfully and have outputs JSONs to compare
            comp = CompareOutputs()
            json_comparison = comp.compare_jsons(self.expected_outputs, output_path)
            unique_keys = len(json_comparison.unique_expected_keys) + len(json_comparison.unique_actual_keys)
            mismatches = len([v for v in json_comparison.key_statuses.values() if v != ComparisonResult.Match])
            status = unique_keys + mismatches  # Will be > 0 if and only if one of the previous lists contains an error/mismatch
            return TestResult(status=status, expect_fail=False, cromwell_fail=False, json_comparison=json_comparison)


@dataclass
class Logger:
    """
    A class for handling writing logs to stdout or provided file.
    """
    writer: argparse.FileType('w')
    indent: str

    def log(self, msg: str, prefix: str = "", indent_level: int = 2) -> None:
        """
        Writes log with proper formating.
        """
        formatted_prefix = f"[{prefix}] " if len(prefix) > 0 else ""
        log_text = f"{formatted_prefix}{indent_level * self.indent}{msg}"
        self.writer.write(f"{log_text}\n")

    def log_test_result(self, test: WDLTest, test_result: TestResult) -> None:
        """
        Writes log for summary report at the end.
        """
        self.log(msg=f"{test.workflow_name}/{test.test_name}", indent_level=2)

        if test_result.expect_fail:
            if test_result.status == 0:
                self.log("Success (expected no outputs)", indent_level=4)
            else:
                self.log("Failure (did not match expectation of failed run)", indent_level=4)
        elif test_result.cromwell_fail:
            self.log("Failure (Cromwell failed to finished unexpectedly)", indent_level=4)
        else:
            json_comparison = test_result.json_comparison
            unique_expected_keys = json_comparison.unique_expected_keys
            unique_actual_keys = json_comparison.unique_actual_keys
            results_to_keys = json_comparison.get_results_to_keys()

            self.log(self.get_log_string_from_results(unique_expected_keys, prefix="Keys unique to expected output"),
                     indent_level=4)
            self.log(self.get_log_string_from_results(unique_actual_keys, prefix="Keys unique to actual output"),
                     indent_level=4)
            self.log(f"Matches: {len(results_to_keys[ComparisonResult.Match])}", indent_level=4)
            self.log(self.get_log_string_from_results(results_to_keys[ComparisonResult.Mismatch], prefix="Mismatches"),
                     indent_level=4)
            self.log(self.get_log_string_from_results(results_to_keys[ComparisonResult.ArrayShapeMismatch],
                                                      prefix="ArrayShapeMismatches"), indent_level=4)
            self.log(self.get_log_string_from_results(results_to_keys[ComparisonResult.FileTypeMismatch],
                                                      prefix="FileTypeMismatches"), indent_level=4)
        self.log("\n")

    def get_log_string_from_results(self, result_list: List[str], prefix: str) -> str:
        """
        Format test results to be used for logging.
        """
        if len(result_list) == 0:
            return f"{prefix}: {len(result_list)}"
        else:
            sep = " -- "
            suffix = " do not match"
            suffix += " <" + 20 * "=" + "!"
            # prefix = "!" + 20*"=" + "> " + prefix
            return f"{prefix}: {len(result_list)}{sep}{' '.join(result_list)}{suffix}"


@dataclass
class WDLTestLog:
    """
    A class wrapping a WDLTest with logging functionality.
    """
    logger: argparse.FileType('w')
    test: WDLTest

    def test_with_logs(self) -> TestResult:
        """
        Run the WDLTest with appropriate logs.
        """
        self.print_startup()
        self.log(f"Running test for {self.test.test_name} and workflow {self.test.workflow_name}...")
        test_result = self.test.run_test()

        result_summary = "Success" if test_result.status == 0 else "Failure"
        self.log(f"Result:", indent_level=2)
        self.log(result_summary, indent_level=4)
        return test_result

    def print_startup(self) -> None:
        """
        Text logged at the start of the test.
        """
        self.log(f"Starting test {self.test.test_name} for workflow {self.test.workflow_name}...")
        self.log(f"Workflow path: {self.test.path}", indent_level=4)
        self.log(f"Test inputs: {self.test.test_inputs}", indent_level=4)
        self.log(f"Expected outputs: {self.test.expected_outputs}", indent_level=4)

    def log(self, msg, indent_level=2) -> None:
        """
        Wrap the underlying logger's log method using prefix attached to specific test run.
        """
        self.logger.log(msg=msg, prefix=f"{self.test.workflow_name}/{self.test.test_name}", indent_level=indent_level)

def check_config_files_exist(config) -> List[FileNotFoundError]:
    """
    Takes a config file chunk and verifies the references files exist.
    """
    errors = []

    path = config['path']
    if not os.path.exists(path):
        errors += [FileNotFoundError(f"Cannot find WDL at path: {path}")]

    test_inputs = config['test_inputs']
    if not os.path.exists(test_inputs):
        errors += [FileNotFoundError(f"Cannot find inputs at path: {test_inputs}")]

    expected_outputs = config['expected_outputs']
    if expected_outputs is not None and not os.path.exists(expected_outputs):
        errors += [FileNotFoundError(f"Cannot find expected outputs at path: {expected_outputs}")]

    return errors

if __name__ == '__main__':
    # Parse args and config
    args = parser.parse_args()
    try:
        yaml = YAML(typ='safe')
        with open(args.config) as file:
            config = yaml.load(file)
    except FileNotFoundError:
        raise FileNotFoundError(f"Cannot find configuration file at path: {args.config}")

    # Check not writing log file with multiprocessing
    assert args.processes == 1 or args.log == sys.stdout, "Writing logs to file not supported in multi-processing mode."

    # Check if user input matches test config
    workflow_names = config.keys()
    test_names = {t for w in config.values() for t in w['tests']}
    wf_test_combos = {(wf, t) for wf, wf_info in config.items() for t in wf_info['tests']}

    # Check input names match files found in config
    requested_workflows = args.workflow if args.workflow is not None else workflow_names
    requested_tests = args.test if args.test is not None else test_names

    for w in requested_workflows:
        if w not in workflow_names:
            raise ValueError (
                f'Requested workflow {w} does not match any workflow in config.  Available workflows are {workflow_names}.'
            )
        for t in requested_tests:
            if (w, t) not in wf_test_combos:
                raise ValueError (
                    f'Requested workflow test {w}:{t} does not match any test workflows in config.  ' +
                    f'Available tests for workflow {w} are {config[w]["tests"]}'
                )


    # Setup Cromwell parameters
    if args.executor is not None:
        cromwell = CromwellConfig(jar_path=args.executor, log_prefix=args.executor_log_prefix)
    else:
        raise ValueError("Must provide -e executor value.")

    # Initialize logger to write outputs
    logger = Logger(writer=args.log, indent=" ")

    # Clean up config paths and restrict to user-provided test/workflow names
    file_errors = []
    test_configs = []
    for workflow, workflow_info in config.items():
        if args.workflow is None or workflow in args.workflow:
            for test, test_info in workflow_info['tests'].items():
                if args.test is None or test in args.test:
                    this_test_config = {'workflow_name': workflow, 'test_name': test}
                    this_test_config['path'] = resolve_relative_path(workflow_info['path'])
                    this_test_config['test_inputs'] = resolve_relative_path(test_info['test_inputs'])
                    this_test_config['expected_outputs'] = (resolve_relative_path(test_info['expected_outputs']) if
                                                            test_info['expected_outputs'] is not None else
                                                            None
                                                            )
                    test_configs.append(this_test_config)

                    file_errors += check_config_files_exist(this_test_config)

    # Stop running and report errors if key files are missing
    for e in file_errors:
        raise e

    # Otherwise continue to collecting/running tests
    tests_to_run = []
    logger.log("Collecting set of tests to run...", indent_level=0)
    for test_config in test_configs:
        test = WDLTest(cromwell_config=cromwell, **test_config)
        tests_to_run += [test]

    logger.log(f"Running tests: {', '.join([t.test_name for t in tests_to_run])}...", indent_level=0)
    test_results = []

    # Actually run the tests either sequentially or concurrently
    logged_tests = [WDLTestLog(logger=logger, test=test) for test in tests_to_run]
    if args.processes > 1:
        run_test_mp = lambda t: t.test_with_logs()  # Define BEFORE Pool is initialized
        pool = mp.Pool(processes=args.processes)
        test_results = pool.map(run_test_mp, logged_tests)
        pool.close()
    else:
        for t in logged_tests:
            # Only print output separator for each test when single process
            logger.log(OUTPUT_SEPARATOR, indent_level=0)
            test_results += [t.test_with_logs()]

    logger.log(OUTPUT_SEPARATOR + "\n", indent_level=0)
    logger.log("Final Test Summary (Workflow Name / Test Name: Result)", indent_level=0)
    for test, result in zip(tests_to_run, test_results):
        logger.log_test_result(test, result)

    any_failed = any([t.status != 0 for t in test_results])
    if any_failed:
        print("Some tests failed. See logs for full summary.")
        exit(1)
    else:
        print("Finished!")
        exit(0)
