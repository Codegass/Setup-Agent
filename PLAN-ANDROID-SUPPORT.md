# Android 项目支持规划

## 概述

本文档规划如何让 Setup-Agent (SAG) 支持 Android 项目的构建和验证。

## 核心发现：Android 构建不需要模拟器

根据技术调研，**大多数 Android 开源项目的 CI/CD 流程不需要模拟器**：

```bash
# 典型的 Android CI 流程（不需要模拟器）
./gradlew assembleDebug     # ✅ 编译 APK
./gradlew test              # ✅ 本地单元测试 (JVM)
./gradlew lint              # ✅ 代码检查
# ./gradlew connectedAndroidTest  # ⚠️ 这个才需要模拟器（可选）
```

### Android 构建成功的标准定义

| 阶段 | Gradle 命令 | 需要模拟器 | 重要性 |
|------|-------------|-----------|--------|
| 编译 | `./gradlew assembleDebug` | ❌ | 🔴 必须 |
| 本地单元测试 | `./gradlew test` | ❌ | 🔴 必须 |
| Lint 检查 | `./gradlew lint` | ❌ | 🟡 建议 |
| **Instrumented Tests** | `./gradlew connectedAndroidTest` | ✅ | 🟢 **可选** |

**结论**：Setup-Agent 的 Docker 方案完全可以处理 Android 项目的"构建成功"验证。

---

## 目标

### MVP 目标（本规划范围）

1. **项目类型识别** - Agent 自动识别项目类型 (android, maven, gradle)
2. **类型感知验证** - `physical_validator.py` 根据项目类型使用不同验证逻辑
3. **类型感知环境** - `docker_orch` 根据项目类型配置不同的 Docker 环境
4. **Android 构建支持** - 编译 APK + 本地单元测试 + Lint

### MVP 功能范围

```
┌─────────────────────────────────────────────────────────────┐
│                   Setup-Agent Docker 容器                    │
│                                                              │
│  ✅ git clone <android-project>                             │
│  ✅ 自动检测项目类型 (Android/Gradle/Maven)                  │
│  ✅ 自动配置 Android SDK 环境                                │
│  ✅ ./gradlew assembleDebug    → 编译 APK                   │
│  ✅ ./gradlew test             → 本地单元测试 (JVM)         │
│  ✅ ./gradlew lint             → 代码质量检查               │
│  ✅ 验证 APK/AAB 是否生成                                    │
│                                                              │
│  这已经是完整的"构建成功"验证！                              │
└─────────────────────────────────────────────────────────────┘
```

---

## Phase 1: 项目类型识别系统

### 1.1 设计理念：Agent 自主分析

**不创建独立的检测工具**，而是让 Agent 使用现有的 `file_io` 工具自己分析项目类型。

好处：
- Agent 更智能，可以处理边缘情况
- 不需要维护额外的检测逻辑
- 更符合 Agent 自主性设计

流程：
```
克隆项目 → Agent 读取配置文件 → Agent 判断类型 → 设置 project_type → 开始构建
                    ↑                    ↑
              使用 file_io          使用 context_tool
              读取 build.gradle     保存类型到 context
              或 pom.xml
```

### 1.2 定义项目类型枚举

**文件**: `config/models.py`

```python
from enum import Enum

class ProjectType(str, Enum):
    """项目类型枚举"""
    ANDROID_APP = "android_app"        # Android 应用项目 (com.android.application)
    ANDROID_LIBRARY = "android_lib"    # Android 库项目 (com.android.library)
    GRADLE_JAVA = "gradle_java"        # 普通 Gradle Java 项目
    GRADLE_KOTLIN = "gradle_kotlin"    # Gradle Kotlin 项目
    MAVEN_JAVA = "maven_java"          # Maven Java 项目
    MAVEN_KOTLIN = "maven_kotlin"      # Maven Kotlin 项目
    UNKNOWN = "unknown"                # 未知类型
```

### 1.3 Agent 分析指南（写入 System Prompt）

Agent 在开始 setup 前**必须**先确定项目类型：

```markdown
## 项目类型分析（必须在 setup 前完成）

在开始构建前，你必须先确定项目类型：

1. 读取项目根目录的配置文件：
   - `build.gradle` 或 `build.gradle.kts` (Gradle 项目)
   - `pom.xml` (Maven 项目)

2. 判断项目类型：
   - 如果 build.gradle 包含 `com.android.application` → android_app
   - 如果 build.gradle 包含 `com.android.library` → android_lib
   - 如果有 build.gradle 但不是 Android → gradle_java 或 gradle_kotlin
   - 如果有 pom.xml → maven_java 或 maven_kotlin

3. 使用 context_tool 保存项目类型：
   context_tool(action="update", key="project_type", value="android_app")

4. 只有设置了 project_type 后才能开始构建。
```

### 1.4 存储位置：environment_summary

项目类型存储在已有的 `TrunkContext.environment_summary` 字段中：

**存储路径**: `/workspace/.setup_agent/contexts/trunk_*.json`

```json
{
  "context_id": "trunk_20250122_123456",
  "goal": "Setup and build project",
  "project_name": "my-android-app",
  "environment_summary": {
    "project_type": "android_app",      // ← 存在这里
    "detected_at": "2025-01-22T12:00:00",
    "detection_evidence": "build.gradle contains com.android.application"
  },
  "todo_list": [...]
}
```

Agent 使用 `context_tool` 更新这个值：
```python
# Agent 分析完项目后调用
context_tool(
    action="update_environment", 
    key="project_type", 
    value="android_app"
)
```

### 1.5 强制检查方案：工具层面拦截

**核心思路**：在 `maven_tool` 和 `gradle_tool` 执行 build 前检查 `project_type`，如果未设置则**拒绝执行并报错**。

这是最可靠的方案，因为：
1. Agent 必须调用工具才能 build
2. 工具拒绝执行后，Agent 必须先解决这个问题
3. 无法绕过

**修改**: `agent/context_manager.py`

```python
class ContextManager:
    def get_project_type(self) -> Optional[str]:
        """从 environment_summary 获取项目类型"""
        trunk_context = self.load_trunk_context()
        if trunk_context and trunk_context.environment_summary:
            return trunk_context.environment_summary.get("project_type")
        return None
    
    def set_project_type(self, project_type: str, evidence: str = "") -> bool:
        """设置项目类型到 environment_summary"""
        trunk_context = self.load_trunk_context()
        if not trunk_context:
            return False
        
        if not trunk_context.environment_summary:
            trunk_context.environment_summary = {}
        
        trunk_context.environment_summary["project_type"] = project_type
        trunk_context.environment_summary["detected_at"] = datetime.now().isoformat()
        if evidence:
            trunk_context.environment_summary["detection_evidence"] = evidence
        
        self._save_trunk_context(trunk_context)
        return True
```

### 1.6 工具层面强制检查（关键）

**问题**：Agent 可能绕过专用工具，直接用 `bash` 执行 `mvn compile` 或 `./gradlew build`。

**解决方案**：三层防护

```
第一层: 专用工具检查 (maven_tool, gradle_tool)
    ↓ 如果绕过
第二层: Bash 工具拦截 (检测构建命令)
    ↓ 如果绕过
第三层: PhysicalValidator 验证时标记
```

#### 第一层：专用工具检查

**修改**: `tools/maven_tool.py` 和 `tools/gradle_tool.py`

```python
class MavenTool(BaseTool):
    def __init__(self, orchestrator, context_manager: ContextManager = None, ...):
        self.context_manager = context_manager
    
    def execute(self, command: str, ...) -> ToolResult:
        # ⚠️ 强制检查
        if self.context_manager:
            project_type = self.context_manager.get_project_type()
            if not project_type:
                raise ToolError(
                    message="❌ 必须先确定项目类型才能执行构建命令",
                    suggestions=[
                        "1. 读取 pom.xml 确定项目类型",
                        "2. 设置 project_type: context_tool(action='update_environment', ...)",
                        "3. 然后再执行 maven 命令"
                    ],
                    error_code="PROJECT_TYPE_NOT_SET"
                )
        # ... 继续执行 ...
```

#### 第二层：Bash 工具拦截

**修改**: `tools/bash.py`

```python
class BashTool(BaseTool):
    # 需要 project_type 的构建命令模式
    BUILD_COMMAND_PATTERNS = [
        r'^mvn\s',           # mvn compile, mvn test, etc.
        r'^\.\/gradlew\s',   # ./gradlew build, etc.
        r'^gradle\s',        # gradle build, etc.
        r'^\.\/mvnw\s',      # Maven wrapper
    ]
    
    def __init__(self, orchestrator, context_manager: ContextManager = None, ...):
        self.context_manager = context_manager
    
    def execute(self, command: str, ...) -> ToolResult:
        # 检查是否是构建命令
        if self._is_build_command(command):
            if self.context_manager:
                project_type = self.context_manager.get_project_type()
                if not project_type:
                    raise ToolError(
                        message=f"❌ 检测到构建命令 '{command[:30]}...'，但项目类型未设置",
                        suggestions=[
                            "请使用专用工具执行构建:",
                            "• Maven 项目: maven(command='compile', ...)",
                            "• Gradle 项目: gradle(tasks='build', ...)",
                            "这些工具提供更好的错误处理和输出格式化",
                            "",
                            "如果必须使用 bash，请先设置项目类型:",
                            "context_tool(action='update_environment', updates={'project_type': 'maven_java'})"
                        ],
                        error_code="BUILD_COMMAND_WITHOUT_TYPE"
                    )
        
        # ... 继续执行 ...
    
    def _is_build_command(self, command: str) -> bool:
        """检测是否是构建命令"""
        import re
        command = command.strip()
        for pattern in self.BUILD_COMMAND_PATTERNS:
            if re.match(pattern, command):
                return True
        return False
```

#### 第三层：验证阶段检查

**修改**: `agent/physical_validator.py`

```python
def validate_build_artifacts(self, project_name: str = None) -> Dict[str, Any]:
    project_type = self.get_project_type()
    
    result = {
        "valid": False,
        "project_type": project_type,
        "project_type_verified": project_type is not None,
        # ...
    }
    
    if not project_type:
        result["warnings"] = result.get("warnings", [])
        result["warnings"].append(
            "⚠️ 项目类型未设置，无法使用类型感知验证，使用通用验证逻辑"
        )
        # 使用通用验证逻辑
        return self._validate_generic_artifacts(project_dir)
    
    # 根据类型选择验证逻辑
    if project_type in ["android_app", "android_lib"]:
        return self._validate_android_artifacts(project_dir)
    # ...
```

### 1.6 Physical Validator 获取类型

**修改**: `agent/physical_validator.py`

```python
class PhysicalValidator:
    def __init__(self, docker_orchestrator=None, context_manager=None, ...):
        self.context_manager = context_manager
    
    def get_project_type(self) -> str:
        """从 context 获取项目类型"""
        if self.context_manager:
            return self.context_manager.get_project_type() or "unknown"
        return "unknown"
    
    def validate_build_artifacts(self, project_name: str = None) -> Dict[str, Any]:
        project_type = self.get_project_type()
        
        if project_type in ["android_app", "android_lib"]:
            return self._validate_android_artifacts(project_dir)
        elif project_type.startswith("gradle_"):
            return self._validate_gradle_artifacts(project_dir)
        elif project_type.startswith("maven_"):
            return self._validate_maven_artifacts(project_dir)
        else:
            # 回退到当前逻辑
            return self._validate_generic_artifacts(project_dir)
```

---

## 附录 A: 当前系统的流程控制机制分析

### 当前如何防止 Agent 跳步骤

```
┌─────────────────────────────────────────────────────────────────┐
│                    TODO List (trunk_context)                     │
│                                                                 │
│  task_1: Clone project          [COMPLETED] ✓                   │
│  task_2: Analyze dependencies   [IN_PROGRESS] ← 当前任务        │
│  task_3: Build project          [PENDING]                       │
│  task_4: Run tests              [PENDING]                       │
│  task_5: Generate report        [PENDING]                       │
└─────────────────────────────────────────────────────────────────┘
```

#### 机制 1: 任务顺序验证 (`TrunkContext`)

```python
# context_manager.py

def can_start_task(self, task_id: str) -> bool:
    """只允许启动下一个 PENDING 任务"""
    next_task = self.get_next_pending_task()
    return next_task is not None and next_task.id == task_id

def get_next_pending_task(self) -> Optional[Task]:
    """获取下一个可执行任务（严格顺序）"""
    for task in self.todo_list:
        if task.status == TaskStatus.PENDING:
            return task
        elif task.status == TaskStatus.IN_PROGRESS:
            return None  # 有进行中的任务，不能启动新任务
        elif task.status == TaskStatus.FAILED:
            return None  # 有失败的任务，不能继续
    return None
```

#### 机制 2: 启动任务时拦截 (`context_tool._start_task`)

```python
# Agent 尝试跳过任务
>>> context_tool(action="start_task", task_id="task_4")

❌ ToolError: Cannot start task task_4. Must complete task_2 first.

建议:
- Check if task ID is correct
- Ensure tasks are executed in order (cannot skip)
- Use get_info to see currently executable task
```

#### 机制 3: 验证任务 ID 存在性

```python
# 防止 Agent 幻觉一个不存在的 task_id
valid_task_ids = [task.id for task in trunk_context.todo_list]
if task_id not in valid_task_ids:
    raise ToolError(
        message=f"Invalid task ID '{task_id}'. This task does not exist.",
        suggestions=["Use one of the valid task IDs: task_1, task_2, ..."]
    )
```

### 当前机制的局限性

| 防护点 | 是否有效 | 说明 |
|--------|---------|------|
| 防止跳过任务 | ✅ 是 | `can_start_task` 强制检查 |
| 防止同时多任务 | ✅ 是 | IN_PROGRESS 时不能启动新任务 |
| 防止幻觉 task_id | ✅ 是 | 验证 ID 存在性 |
| **强制使用 context_tool** | ❌ 否 | Agent 可以不用 context_tool 直接工作 |
| **强制先分析再构建** | ❌ 否 | 没有这个检查 |

### 类比：项目类型检查的设计

当前的任务顺序控制是**被动防护** - 只在 Agent 调用 `context_tool` 时检查。

项目类型检查需要**主动防护** - 在 Agent 调用 build 工具时强制检查：

```
当前任务控制:
  Agent 调用 context_tool(start_task) → 检查顺序 → 允许/拒绝

项目类型检查 (我们要做的):
  Agent 调用 maven/gradle/bash → 检查 project_type → 允许/拒绝
```

这就是为什么我们需要在 maven_tool、gradle_tool、bash_tool 中添加检查。

---

## Phase 2: 类型感知的 Physical Validator

### 2.1 重构验证器架构

**当前架构**:
```
PhysicalValidator
└── validate_build_artifacts() → 只检查 .class 和 .jar
```

**新架构**:
```
PhysicalValidator
├── project_type: ProjectType
├── validate_build_artifacts()
│   ├── _validate_android_artifacts()    # APK, AAB
│   ├── _validate_gradle_artifacts()     # JAR, classes
│   └── _validate_maven_artifacts()      # JAR, classes
└── parse_test_reports()
    ├── _parse_android_unit_test_reports()  # Android Unit Test XML
    ├── _parse_gradle_test_reports()        # Gradle Test XML
    └── _parse_maven_test_reports()         # Surefire XML
```

### 2.2 Android 构建产物验证

```python
def _validate_android_artifacts(self, project_dir: str) -> Dict[str, Any]:
    """验证 Android 构建产物"""
    
    validation_result = {
        "valid": False,
        "apk_files": [],      # Debug/Release APKs
        "aab_files": [],      # Android App Bundles
        "recent_build": False,
        "evidence": []
    }
    
    # 1. 检查 APK 文件
    # 路径: build/outputs/apk/debug/*.apk
    #       build/outputs/apk/release/*.apk
    #       app/build/outputs/apk/debug/*.apk (多模块项目)
    
    # 2. 检查 AAB 文件 (可选)
    # 路径: build/outputs/bundle/release/*.aab
    
    # 3. 检查编译时间戳
    
    return validation_result
```

### 2.3 Android 单元测试报告解析

**注意**：MVP 只解析本地单元测试报告，不包含 Instrumented Tests。

```python
def _parse_android_unit_test_reports(self, project_dir: str) -> Dict[str, Any]:
    """解析 Android 本地单元测试报告"""
    
    result = {
        "unit_tests": {
            "total": 0,
            "passed": 0,
            "failed": 0,
            "skipped": 0
        }
    }
    
    # 解析 build/reports/tests/testDebugUnitTest/
    # 或    app/build/reports/tests/testDebugUnitTest/ (多模块)
    
    return result
```

---

## Phase 3: 类型感知的 Docker 环境

### 3.1 Docker 镜像策略

| 项目类型 | 推荐镜像 | 说明 |
|---------|---------|------|
| MAVEN_* | `maven:3.9-eclipse-temurin-17` | 官方 Maven 镜像 |
| GRADLE_* | `gradle:8-jdk17` | 官方 Gradle 镜像 |
| ANDROID_* | `自建镜像` 或 `cimg/android:*` | 需要 Android SDK |

### 3.2 Android Docker 镜像配置

**文件**: `docker/Dockerfile.android`

```dockerfile
FROM ubuntu:22.04

# 基础工具
RUN apt-get update && apt-get install -y \
    curl wget unzip git \
    openjdk-17-jdk \
    && rm -rf /var/lib/apt/lists/*

# 环境变量
ENV ANDROID_HOME=/opt/android-sdk
ENV ANDROID_SDK_ROOT=$ANDROID_HOME
ENV PATH=$PATH:$ANDROID_HOME/cmdline-tools/latest/bin:$ANDROID_HOME/platform-tools

# 安装 Android SDK command-line tools
RUN mkdir -p $ANDROID_HOME/cmdline-tools && \
    cd $ANDROID_HOME/cmdline-tools && \
    wget -q https://dl.google.com/android/repository/commandlinetools-linux-11076708_latest.zip && \
    unzip -q commandlinetools-linux-*.zip && \
    mv cmdline-tools latest && \
    rm commandlinetools-linux-*.zip

# 接受 SDK 许可证
RUN yes | sdkmanager --licenses

# 安装必要的 SDK 组件
RUN sdkmanager \
    "platform-tools" \
    "platforms;android-34" \
    "build-tools;34.0.0" \
    "extras;google;m2repository" \
    "extras;android;m2repository"

WORKDIR /workspace
```

### 3.3 修改 Docker Orchestrator

**修改**: `docker_orch/orch.py`

```python
class DockerOrchestrator:
    # 镜像映射
    PROJECT_TYPE_IMAGES = {
        ProjectType.ANDROID_APP: "sag-android:latest",
        ProjectType.ANDROID_LIBRARY: "sag-android:latest",
        ProjectType.GRADLE_JAVA: "gradle:8-jdk17",
        ProjectType.GRADLE_KOTLIN: "gradle:8-jdk17",
        ProjectType.MAVEN_JAVA: "maven:3.9-eclipse-temurin-17",
        ProjectType.MAVEN_KOTLIN: "maven:3.9-eclipse-temurin-17",
        ProjectType.UNKNOWN: "ubuntu:22.04",
    }
    
    def __init__(self, project_type: ProjectType = None, ...):
        self.project_type = project_type
        if project_type:
            self.base_image = self.PROJECT_TYPE_IMAGES.get(
                project_type, 
                self.config.docker_base_image
            )
```

---

## 实现任务清单

### P0: 必须完成（三层强制检查机制）

- [ ] **T1**: 创建 `ProjectType` 枚举 (`config/models.py`)
- [ ] **T2**: 添加 `get_project_type()` 和 `set_project_type()` 到 `ContextManager`
- [ ] **T3**: **第一层** - 在 `MavenTool.execute()` 开头添加 project_type 检查
- [ ] **T4**: **第一层** - 在 `GradleTool.execute()` 开头添加 project_type 检查
- [ ] **T5**: **第二层** - 在 `BashTool.execute()` 中检测构建命令并拦截
- [ ] **T6**: 更新 `context_tool` 支持 `update_environment` action 设置 project_type
- [ ] **T7**: 添加项目类型分析指南到 System Prompt

### P1: Android 支持

- [ ] **T7**: 添加 Android 产物验证到 `PhysicalValidator` (APK/AAB)
- [ ] **T8**: 创建 Android Docker 镜像配置
- [ ] **T9**: 修改 `DockerOrchestrator` 支持类型感知镜像选择
- [ ] **T10**: 添加 Android 单元测试报告解析

### P2: 优化

- [ ] **T11**: 更新 `GradleTool` 支持 Android 特有任务 (`assembleDebug`, `lint`)
- [ ] **T12**: 工具类型匹配警告 (如 Android 项目用 maven 工具)

---

## 文件变更清单

### 新增文件

| 文件 | 描述 |
|------|------|
| `docker/Dockerfile.android` | Android SDK 镜像 |
| `docker/Dockerfile.gradle` | Gradle 镜像 (可选) |
| `docker/Dockerfile.maven` | Maven 镜像 (可选) |

### 修改文件

| 文件 | 修改内容 |
|------|---------|
| `config/models.py` | 添加 `ProjectType` 枚举 |
| `config/settings.py` | 添加镜像配置 |
| `agent/physical_validator.py` | 添加类型感知验证 (Android APK/AAB)，从 context 读取 project_type |
| `agent/context_manager.py` | 添加 `get_project_type()` + `validate_setup_prerequisites()` |
| `agent/react_engine.py` | 更新 System Prompt 添加项目类型分析指南 |
| `tools/context_tool.py` | 确保 `update_environment` action 支持存储 project_type |
| `docker_orch/orch.py` | 添加类型感知镜像选择 |
| `tools/gradle_tool.py` | 添加 Android 任务支持 |

---

## MVP 对用户的影响

```
✅ 可以做的:
  • 克隆 Android 项目
  • 自动检测项目类型
  • 自动配置 Android SDK 环境
  • 编译 Debug/Release APK
  • 运行本地单元测试 (./gradlew test)
  • 代码质量检查 (./gradlew lint)
  • 验证构建产物 (APK/AAB)

❌ 不支持 (见 TODO):
  • Instrumented Tests (需要模拟器)
  • 签名发布 (需要 keystore)
```

---

## TODO: 未来功能 (不在 MVP 范围)

### TODO-1: Instrumented Tests 支持

**背景**：Instrumented Tests (`./gradlew connectedAndroidTest`) 需要 Android 模拟器或真机。

**技术限制**：
- Docker 内运行模拟器在 macOS Apple Silicon 上**完全不可能**
- 即使在 Linux 上也需要 KVM 支持且不稳定
- 详见下方技术调研

**可选方案**：

| 方案 | 描述 | 复杂度 |
|------|------|--------|
| 方案 A: 主机模拟器 | Docker 通过 ADB over TCP 连接主机模拟器 | 中 |
| 方案 B: Firebase Test Lab | 上传 APK 到云端测试 | 中 |
| 方案 C: 用户自行处理 | 文档说明，不集成 | 低 |

**实现任务**：
- [ ] **TODO-T1**: 检测主机 ADB 和模拟器可用性
- [ ] **TODO-T2**: 实现 ADB over TCP 连接
- [ ] **TODO-T3**: 添加 Instrumented Tests 报告解析
- [ ] **TODO-T4**: Firebase Test Lab 集成 (可选)

### TODO-2: 签名发布支持

- [ ] **TODO-T5**: Keystore 配置管理
- [ ] **TODO-T6**: Release APK/AAB 签名

---

## 技术调研附录

### Instrumented Tests 平台兼容性 (2025年1月调研)

| 平台 | Docker 内模拟器 | 可行性 | 说明 |
|------|----------------|--------|------|
| Linux (原生) | ⚠️ 需要 KVM | 不稳定 | `--privileged --device /dev/kvm` |
| Linux (云服务器) | ❌ 通常禁用 KVM | 不可行 | 大多数禁用嵌套虚拟化 |
| macOS Intel | ⚠️ 嵌套虚拟化 | 性能极差 | Docker VM 内再跑 VM |
| **macOS Apple Silicon** | ❌ | **不可能** | Rosetta 无法模拟 KVM |
| Windows WSL2 | ⚠️ 需要配置 | 复杂 | Hyper-V 嵌套虚拟化 |

### 调研来源

1. **budtmo/docker-android**: https://github.com/budtmo/docker-android
   - 已知问题: Android 13/14 崩溃、数据持久化、稳定性

2. **GitHub Actions KVM 支持** (2024年4月):
   - https://github.blog/changelog/2024-04-02-github-actions-hardware-accelerated-android-virtualization-now-available/

3. **Firebase Test Lab**: https://firebase.google.com/docs/test-lab
   - 虚拟设备 $1/小时，支持 gcloud CLI

4. **Android 官方文档**:
   - https://developer.android.com/training/testing/instrumented-tests
   - 推荐只在必要时使用 instrumented tests

---

## 变更历史

| 日期 | 版本 | 变更内容 |
|------|------|---------|
| 2025-01-22 | v1.0 | 初始规划 |
| 2025-01-22 | v1.1 | 简化 MVP，Instrumented Tests 移入 TODO |
