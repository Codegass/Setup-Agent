"""
Test Case Catalog Module for Unified Test Tracking

This module provides dataclasses and utilities for consistent test case tracking
across static analysis (expected tests) and runtime validation (executed tests).
It ensures deduplication and enables meaningful comparison between what tests
exist in code vs what actually ran.
"""

import json
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple, Any
from pathlib import Path
from loguru import logger


@dataclass
class TestCaseDescriptor:
    """Describes a test case discovered via static analysis.

    Attributes:
        package: Java package name (e.g., 'com.example.tests')
        class_name: Test class name (e.g., 'UserServiceTest')
        method_name: Test method name (e.g., 'testCreateUser')
        file_path: Path to source file relative to project root
        module: Optional module name for multi-module projects
    """
    package: str
    class_name: str
    method_name: str
    file_path: str
    module: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "package": self.package,
            "class_name": self.class_name,
            "method_name": self.method_name,
            "file_path": self.file_path,
            "module": self.module
        }


@dataclass
class RuntimeTestCaseRecord:
    """Records runtime execution information for a test case.

    Attributes:
        descriptor: TestCaseDescriptor if resolved, else minimal info
        key: Normalized test case key for deduplication
        statuses: List of all observed statuses (for tracing)
        final_status: Most severe status (error > failed > skipped > passed)
        execution_time_ms: Total execution time across all runs
        sources: Set of report file paths this test appeared in
        raw_names: Original test names before normalization (for debugging)
    """
    descriptor: Optional[TestCaseDescriptor]
    key: str
    statuses: List[str] = field(default_factory=list)
    final_status: str = "passed"
    execution_time_ms: float = 0.0
    sources: Set[str] = field(default_factory=set)
    raw_names: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "descriptor": self.descriptor.to_dict() if self.descriptor else None,
            "key": self.key,
            "statuses": self.statuses,
            "final_status": self.final_status,
            "execution_time_ms": self.execution_time_ms,
            "sources": list(self.sources),
            "raw_names": self.raw_names[:5]  # Limit for memory
        }


class TestCaseCatalog:
    """Container for test case descriptors discovered via static analysis."""

    def __init__(self):
        self._descriptors: Dict[str, TestCaseDescriptor] = {}
        self._by_class: Dict[str, List[str]] = {}
        self._by_module: Dict[str, List[str]] = {}

    def add(self, descriptor: TestCaseDescriptor) -> str:
        """Add a test case descriptor and return its key."""
        key = generate_test_case_key(descriptor)
        self._descriptors[key] = descriptor

        # Index by class
        class_key = f"{descriptor.package}.{descriptor.class_name}"
        if class_key not in self._by_class:
            self._by_class[class_key] = []
        self._by_class[class_key].append(key)

        # Index by module if applicable
        if descriptor.module:
            if descriptor.module not in self._by_module:
                self._by_module[descriptor.module] = []
            self._by_module[descriptor.module].append(key)

        return key

    def get(self, key: str) -> Optional[TestCaseDescriptor]:
        """Get descriptor by key."""
        return self._descriptors.get(key)

    def get_all(self) -> Dict[str, TestCaseDescriptor]:
        """Get all descriptors."""
        return self._descriptors.copy()

    def get_by_class(self, package: str, class_name: str) -> List[TestCaseDescriptor]:
        """Get all test methods in a class."""
        class_key = f"{package}.{class_name}"
        keys = self._by_class.get(class_key, [])
        return [self._descriptors[k] for k in keys if k in self._descriptors]

    def get_by_module(self, module: str) -> List[TestCaseDescriptor]:
        """Get all tests in a module."""
        keys = self._by_module.get(module, [])
        return [self._descriptors[k] for k in keys]

    def count(self) -> int:
        """Total number of test cases."""
        return len(self._descriptors)

    def to_dict(self) -> Dict[str, Any]:
        """Convert catalog to dictionary for serialization."""
        return {
            "total_count": self.count(),
            "by_module": {
                module: len(keys) for module, keys in self._by_module.items()
            },
            "descriptors": {
                key: desc.to_dict()
                for key, desc in self._descriptors.items()
            }
        }


def generate_test_case_key(descriptor: TestCaseDescriptor) -> str:
    """Generate a canonical key for a test case descriptor.

    Format: {package}.{class_name}::{method_name}
    Normalizes method name to strip parameter decorations.
    """
    method = normalize_method_name(descriptor.method_name)
    return f"{descriptor.package}.{descriptor.class_name}::{method}"


def normalize_testcase_identifier(
    classname: Optional[str],
    name: Optional[str],
    file_path: Optional[str] = None
) -> Optional[str]:
    """Normalize a test case identifier from runtime XML data.

    This handles various formats seen in test reports:
    - Parameterized: testMethod[0], testMethod(param=value)
    - Display names: testMethod (Display Name)
    - Nested classes: OuterClass$InnerClass

    Returns:
        Normalized key in format: {classname}::{method_name}
        Returns None if insufficient data.
    """
    if not name:
        return None

    method_name = normalize_method_name(name)
    if not method_name:
        return None

    # Clean up classname
    if classname:
        # Handle nested classes
        classname = classname.replace('$', '.')
        classname = classname.strip()
    elif file_path:
        # Try to derive from file path as fallback
        classname = file_path.replace('/', '.').replace('.java', '').replace('.class', '')
        classname = classname.strip('.')
    else:
        # Can't create a proper key without class context
        return method_name

    return f"{classname}::{method_name}"


def normalize_method_name(method_name: str) -> str:
    """Strip parameter decorations and indices from test method names.

    Handles:
    - JUnit parameterized: testMethod[0], testMethod[1]
    - JUnit5 display names: testMethod(String, int)[1]
    - TestNG data providers: testMethod(param1=value1)
    - Spock/Groovy: testMethod #0, testMethod #1
    """
    if not method_name:
        return ""

    # Remove trailing whitespace
    name = method_name.strip()

    # Strip parameterized test indices and parameters
    # Handle [index] format
    if '[' in name:
        name = name.split('[')[0]

    # Handle (parameters) format
    if '(' in name:
        name = name.split('(')[0]

    # Handle Spock-style #index format
    if ' #' in name:
        name = name.split(' #')[0]

    # Don't remove trailing digits - they may be part of the method name!
    # Only clean up trailing whitespace
    return name.strip()


def merge_testcase_status(current: str, new: str) -> str:
    """Merge test case statuses, keeping the most severe.

    Severity order: error > failed > skipped > passed
    """
    severity = {"passed": 0, "skipped": 1, "failed": 2, "error": 3}
    return new if severity.get(new, 0) > severity.get(current, 0) else current


def build_java_test_catalog(project_path: str, docker_orchestrator) -> TestCaseCatalog:
    """Build a catalog of Java test cases via static analysis.

    This function scans Java test files and extracts test methods with their
    full context (package, class, method, file path). It handles:
    - JUnit 4/5 @Test annotations
    - TestNG @Test annotations
    - Parameterized tests
    - Test factories and templates

    Args:
        project_path: Root directory of the Java project
        docker_orchestrator: Docker orchestrator for command execution

    Returns:
        TestCaseCatalog containing all discovered test cases
    """
    catalog = TestCaseCatalog()

    if not docker_orchestrator:
        logger.warning("No docker orchestrator available for test discovery")
        return catalog

    try:
        # Use the existing annotation counting script but enhance it to return full details
        command = f"""cd {project_path} && python3 - <<'PY'
import json
import re
from pathlib import Path

EXCLUDED_DIR_NAMES = {{
    '.git', '.svn', '.idea', '.vscode',
    'target', 'build', 'out', 'tmp'
}}

def extract_package(content):
    match = re.search(r'^package\\s+([a-zA-Z0-9_.]+);', content, re.MULTILINE)
    return match.group(1) if match else ''

def extract_class_name(content, file_name):
    # Try to find public class first
    match = re.search(r'public\\s+class\\s+([A-Za-z0-9_]+)', content)
    if match:
        return match.group(1)
    # Fall back to any class
    match = re.search(r'class\\s+([A-Za-z0-9_]+)', content)
    if match:
        return match.group(1)
    # Use filename as last resort
    return Path(file_name).stem

def extract_test_methods(content):
    # Find all @Test, @ParameterizedTest, @RepeatedTest, etc.
    # Improved pattern to handle various test annotation styles and modifiers
    # Handles: @Test, @Test(...), annotations on separate lines, various method modifiers
    methods = []

    # Primary pattern for common test annotations
    pattern = r'@(Test|ParameterizedTest|RepeatedTest|TestFactory|TestTemplate|DataProvider)(?:\([^)]*\))?\\s*(?:.*?\\s+)?(?:public\\s+)?(?:void\\s+)?([a-zA-Z0-9_]+)\\s*\\('
    matches = re.findall(pattern, content, re.DOTALL)
    methods.extend([method_name for _, method_name in matches])

    # Secondary pattern to catch tests where annotation is on a different line
    # Look for public void methods that likely are tests (common test method pattern)
    lines = content.split('\\n')
    for i, line in enumerate(lines):
        if '@Test' in line or '@ParameterizedTest' in line or '@RepeatedTest' in line:
            # Look ahead for the method declaration (within next 5 lines)
            for j in range(i+1, min(i+6, len(lines))):
                method_match = re.search(r'(?:public\\s+)?(?:void\\s+)?([a-zA-Z0-9_]+)\\s*\\(', lines[j])
                if method_match:
                    method_name = method_match.group(1)
                    if method_name not in methods and not method_name.startswith('set') and not method_name.startswith('get'):
                        methods.append(method_name)
                    break

    # Remove duplicates while preserving order
    seen = set()
    unique_methods = []
    for method in methods:
        if method not in seen:
            seen.add(method)
            unique_methods.append(method)

    return unique_methods

def is_excluded(path):
    return any(part in EXCLUDED_DIR_NAMES for part in path.parts)

project_root = Path('.')
test_cases = []

# Find all test directories
test_dirs = []
for candidate in project_root.rglob('src'):
    if candidate.name != 'src':
        continue
    test_dir = candidate / 'test' / 'java'
    if not test_dir.is_dir():
        test_dir = candidate / 'test'
        if not test_dir.is_dir():
            continue
    if is_excluded(test_dir):
        continue
    test_dirs.append(test_dir)

# Process each test file
for test_dir in test_dirs:
    for java_file in test_dir.rglob('*.java'):
        if is_excluded(java_file.parent):
            continue

        # Determine module from path
        module = None
        parts = java_file.parts
        if 'src' in parts:
            src_idx = parts.index('src')
            if src_idx > 0:
                # Module is the directory containing src
                module = parts[src_idx - 1]
                if module == '.':
                    module = None

        try:
            content = java_file.read_text(encoding='utf-8')
        except:
            try:
                content = java_file.read_text(encoding='latin-1')
            except:
                continue

        package = extract_package(content)
        class_name = extract_class_name(content, java_file.name)
        methods = extract_test_methods(content)

        if methods:
            file_path = str(java_file.relative_to(project_root))
            for method in methods:
                test_cases.append({{
                    'package': package,
                    'class_name': class_name,
                    'method_name': method,
                    'file_path': file_path,
                    'module': module
                }})

print(json.dumps({{'test_cases': test_cases, 'total': len(test_cases)}}))
PY"""

        result = docker_orchestrator.execute_command(command)
        if not result.get("success"):
            logger.warning(f"Failed to discover Java tests: {result.get('error', 'Unknown error')}")
            return catalog

        output = result.get("output", "").strip()
        if not output:
            logger.info("No Java test cases found in project")
            return catalog

        try:
            data = json.loads(output.splitlines()[-1])
            test_cases = data.get('test_cases', [])

            for tc in test_cases:
                descriptor = TestCaseDescriptor(
                    package=tc.get('package', ''),
                    class_name=tc.get('class_name', ''),
                    method_name=tc.get('method_name', ''),
                    file_path=tc.get('file_path', ''),
                    module=tc.get('module')
                )
                catalog.add(descriptor)

            logger.info(f"📊 Built test catalog with {catalog.count()} test methods")

            # Log module breakdown if multi-module
            by_module = catalog.to_dict()['by_module']
            if by_module and len(by_module) > 1:
                logger.info(f"   Multi-module breakdown: {by_module}")

        except (json.JSONDecodeError, KeyError) as e:
            logger.warning(f"Failed to parse test discovery output: {e}")

    except Exception as e:
        logger.error(f"Error building Java test catalog: {e}")

    return catalog