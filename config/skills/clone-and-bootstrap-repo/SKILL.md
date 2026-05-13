---
name: clone-and-bootstrap-repo
description: Use at the start of every project setup to clone the repo, identify the build system, and ensure the correct toolchain (e.g. Java version) is installed before you try to build.
---

# Clone and bootstrap a fresh repository

Use this skill the first time you touch a repo. It replaces the ad-hoc
"clone, then guess Java version, then maybe install JDK" dance.

## Steps

1. **Clone the repository.**
   - Prefer `bash(command="git clone <url> /workspace/<dir>")` for a minimal,
     observable clone. Use `project_setup(action="clone", repository_url=...)`
     only if you also want SAG's legacy auto-detect side-effects.
   - Verify: `bash(command="ls /workspace/<dir>/.git")` should succeed.

2. **Identify the build system.**
   Run `bash(command="ls /workspace/<dir>")` and look for one of:
   - `pom.xml` (root) or `*/pom.xml` (multi-module) → Maven
   - `build.gradle` or `build.gradle.kts` → Gradle
   - `package.json` → Node.js (npm/pnpm/yarn)
   - `pyproject.toml` / `requirements.txt` → Python
   - `Cargo.toml` → Rust
   - `go.mod` → Go

3. **For Java projects, detect the required JDK version.**
   - Maven (root pom or parent pom):
     - `bash(command="grep -E 'maven.compiler.(release|target|source)|RequireJavaVersion' pom.xml */pom.xml 2>/dev/null | head")`
     - Priority: `maven.compiler.release` > `maven.compiler.target` > `maven.compiler.source` > `java.version`
     - Maven Enforcer plugin (`RequireJavaVersion`) wins if present.
   - Gradle:
     - `bash(command="grep -E 'sourceCompatibility|targetCompatibility|languageVersion' build.gradle build.gradle.kts 2>/dev/null")`

4. **Install the right JDK if it isn't already.**
   - Check: `bash(command="java -version 2>&1")`
   - If absent or wrong major version: `system(action="install_java", java_version="<N>")`
     where `<N>` is `8`, `11`, `17`, `21`, etc.
   - Verify: `java -version` should report the requested major version *and*
     `echo $JAVA_HOME` should be set.

5. **For non-Java projects, install the runtime you actually need.**
   - Node.js: `bash(command="node --version")` — if missing, `system(action="install", packages="nodejs npm")`.
   - Python: usually present; verify with `python3 --version`.
   - Don't preemptively `apt-get update` — that's already done at container init.

## Verification (do not skip)

Before declaring this skill complete:
- `bash(command="ls /workspace/<dir>")` shows the cloned tree
- For Java: `java -version 2>&1` reports the right major version
- For others: the language runtime command succeeds

Then proceed to `project_analyzer(action="analyze", project_path="/workspace/<dir>")`
to derive a project-specific task plan.

## Anti-patterns

- **Don't** call `project_setup(action='install_dependencies')` blindly — it
  has overlapping responsibilities with this skill and `system_tool`.
- **Don't** assume the default `default-jdk` package is correct. Tika needs 17;
  Struts needs 8 or 11; older Spring projects need 8.
- **Don't** run `mvn` or `gradle` before verifying the JDK — version mismatch
  errors are noisy and waste iterations.
