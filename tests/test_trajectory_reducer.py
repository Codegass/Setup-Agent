"""The reducer folds real control events into turns, and never raises.

Every ARCHIVED fixture below is bytes lifted verbatim out of a recorded session
— the kafka d2r3 run at
`logs/session_20260814_072758_651456_4d81644227c3_24117/control_events.jsonl`,
for the controller turn the httpcomponents-client run at
`logs/session_20260813_202604_931286_731b3b90c419_2417/control_events.jsonl`,
for the refused call the camel-quarkus d2r3 run at
`logs/session_20260814_093336_763203_12dba3497295_26013/control_events.jsonl`,
and for the silently replaced gate the rocketmq-externals run at
`logs/session_20260813_203227_051092_171f42548985_2604/control_events.jsonl`.
The field names the reducer joins on are the field names those runs actually
wrote; nothing there is invented.

A handful of fixtures are marked SYNTHETIC. They exist because no archived
ledger exercises the path at all: `gate_outcome_revised` was never emitted by
any of the 27 recorded runs, and the orphan branches fire only on an ordering
no complete session produces. Each synthetic line is built from the payload
class in `sag.agent.control_events` that the engine would seal — and
`test_the_synthetic_gate_lines_are_the_shape_the_engine_would_seal` validates
them against that class, so a fixture cannot drift into a shape the engine
could never write. The DEGRADED lines are the exception, and deliberately so:
they are payloads this schema version cannot read whole, which is the input the
"warnings, never exceptions" rule exists for.
"""

import json

import pytest

from sag.agent.control_events import (
    GateDecisionPayload,
    GateOutcomeRevisedPayload,
    TurnRecordPayload,
)
from sag.trajectory.reducer import DeltaAccumulator, TrajectoryReducer

# Sequences 3, 4 and 6 — one clone call: its envelope, its result, its decision.
# Note the order the engine actually writes: the envelope opens the call, the
# result answers it, and the loop decision lands afterwards.
REAL_TRIPLE_JSONL = """\
{"event_id":"control-000003","kind":"action_envelope","payload":{"action_fingerprint":"act-f5022c8ed4a0cb7760bf22f70f935eff67aef0c2815909ff237afa00be6709d9","envelope_id":"envelope-000003","envelope_sha256":"60afbe3f081a3afd7160a28414d30fe0af30687ac146da84ad451c3c0a207af7","exact_params":{"action":"clone","ref":"4.3.1","repo_url":"https://github.com/apache/kafka.git"},"intent_id":"intent-7dd00da83290","intent_source":"model","tool":"project","tool_call_id":"call_9y6T9c2InqMz0sMOfhRNZJFF"},"sequence":3,"source":null,"timestamp":"2026-08-14T11:28:36.915154Z"}
{"event_id":"control-000004","kind":"tool_result","payload":{"actual_executions":[{"execution_id":"execution_9fb13d5ed84a4597999308f743f3c078","params":{"action":"clone","ref":"4.3.1","repo_url":"https://github.com/apache/kafka.git"},"result":{"conflicts":[],"evidence_assessment":"success","evidence_refs":[],"evidence_status":"verified","facts":{},"invocation_status":"completed","metadata":{"clone_path":"/workspace/kafka","duration_ms":11992.851972579956,"java_version_required":null,"project_type":{"build_files":["/workspace/kafka/committer-tools/requirements.txt","/workspace/kafka/docker/requirements.txt","/workspace/kafka/release/requirements.txt","/workspace/kafka/build.gradle"],"dependencies":[],"language":"java","suggested_tools":["gradle","bash"],"type":"gradle"},"ref":"4.3.1","repository_url":"https://github.com/apache/kafka.git","resolved_commit":"26b251a451ce941d3d7a55e6487bcb7f16b5ad48","target_directory":"kafka"},"operation_outcome":"success","output":"stored as output_6163859b019d","output_ref":"output_6163859b019d","refs":[],"validator_findings":[]},"roles":[],"scope":"environment","tool":"project"}],"envelope_id":"envelope-000003","execution_id":"execution_9fb13d5ed84a4597999308f743f3c078","output_sha256":"1613a8a5d81bc0c63d598b4c6cf3078d64d3930bd788237b03a0968d26210dc4","params":{"action":"clone","ref":"4.3.1","repo_url":"https://github.com/apache/kafka.git"},"result":{"conflicts":[],"evidence_assessment":"success","evidence_refs":[],"evidence_status":"verified","facts":{},"invocation_status":"completed","metadata":{"clone_path":"/workspace/kafka","duration_ms":11992.851972579956,"java_version_required":null,"project_type":{"build_files":["/workspace/kafka/committer-tools/requirements.txt","/workspace/kafka/docker/requirements.txt","/workspace/kafka/release/requirements.txt","/workspace/kafka/build.gradle"],"dependencies":[],"language":"java","suggested_tools":["gradle","bash"],"type":"gradle"},"ref":"4.3.1","repository_url":"https://github.com/apache/kafka.git","resolved_commit":"26b251a451ce941d3d7a55e6487bcb7f16b5ad48","target_directory":"kafka"},"operation_outcome":"success","output":"stored as output_6163859b019d","output_ref":"output_6163859b019d","refs":[],"validator_findings":[]},"roles":[],"scope":"environment","source_attempt_id":"provision-1","source_phase":"provision","tool":"project"},"sequence":4,"source":null,"timestamp":"2026-08-14T11:28:49.545505Z"}
{"event_id":"control-000006","kind":"loop_decision","payload":{"event":{"args":{"action":"clone","ref":"4.3.1","repo_url":"https://github.com/apache/kafka.git"},"attempt_id":"provision-1","error_code":"","evidence_ref":"output_6163859b019d","failure_signature":"","invocation_status":"completed","iteration":1,"job_id":"","operation_outcome":"success","output_cursor":"","phase":"provision","recurrence_count":1,"relevant_scopes":[],"relevant_state":{"artifacts":0,"dependencies":0,"environment":0,"project_analysis":0,"test_runtime":0},"tool_name":"project"},"expected_decision":"continue","expected_reason_code":"outcome_not_loop_candidate"},"sequence":6,"source":null,"timestamp":"2026-08-14T11:28:49.760291Z"}
"""

# Sequences 6 and 10 — two loop decisions with neither envelope nor result.
REAL_TWO_DECISIONS_JSONL = """\
{"event_id":"control-000006","kind":"loop_decision","payload":{"event":{"args":{"action":"clone","ref":"4.3.1","repo_url":"https://github.com/apache/kafka.git"},"attempt_id":"provision-1","error_code":"","evidence_ref":"output_6163859b019d","failure_signature":"","invocation_status":"completed","iteration":1,"job_id":"","operation_outcome":"success","output_cursor":"","phase":"provision","recurrence_count":1,"relevant_scopes":[],"relevant_state":{"artifacts":0,"dependencies":0,"environment":0,"project_analysis":0,"test_runtime":0},"tool_name":"project"},"expected_decision":"continue","expected_reason_code":"outcome_not_loop_candidate"},"sequence":6,"source":null,"timestamp":"2026-08-14T11:28:49.760291Z"}
{"event_id":"control-000010","kind":"loop_decision","payload":{"event":{"args":{"action":"provision","java_version":"17"},"attempt_id":"provision-1","error_code":"","evidence_ref":"output_b201b88114f5","failure_signature":"","invocation_status":"completed","iteration":1,"job_id":"","operation_outcome":"success","output_cursor":"","phase":"provision","recurrence_count":1,"relevant_scopes":[],"relevant_state":{"artifacts":0,"dependencies":0,"environment":0,"project_analysis":0,"test_runtime":0},"tool_name":"project"},"expected_decision":"continue","expected_reason_code":"outcome_not_loop_candidate"},"sequence":10,"source":null,"timestamp":"2026-08-14T11:29:02.207244Z"}
"""

# Sequences 11, 12 and 13 — the env call that failed ENV_EXECUTABLE_NOT_FOUND.
REAL_FAILED_CALL_JSONL = """\
{"event_id":"control-000011","kind":"action_envelope","payload":{"action_fingerprint":"act-a02d161c1ec6f6b8537f5876a4230e8e441d7bc963b6ff04bf5ff81d69410dcc","envelope_id":"envelope-000011","envelope_sha256":"1f3c1764e9e52e9d9690e7d1865baa463736c7223705e2c2e9edf41d5b46de37","exact_params":{"action":"env","activate":true,"executable":"/usr/bin/gradle","requirement":"[8,)","tool":"gradle"},"intent_id":"intent-2afbe116f199","intent_source":"model","tool":"project","tool_call_id":"call_1WoTjiPZJQYRpwoDhxRapdus"},"sequence":11,"source":null,"timestamp":"2026-08-14T11:29:03.780151Z"}
{"event_id":"control-000012","kind":"tool_result","payload":{"actual_executions":[{"execution_id":"execution_f80c78c18ef2416f85b506e781c5659b","params":{"action":"env","activate":true,"executable":"/usr/bin/gradle","requirement":"[8,)","tool":"gradle"},"result":{"conflicts":[],"error":"Env overlay executable is not executable or does not exist: /usr/bin/gradle","error_code":"ENV_EXECUTABLE_NOT_FOUND","error_tail_preview":"Env overlay executable is not executable or does not exist: /usr/bin/gradle","evidence_assessment":"blocked","evidence_refs":[],"evidence_status":"verified","facts":{},"failure_signature":"ENV_EXECUTABLE_NOT_FOUND:836b0507d4ef8e5a","invocation_status":"completed","metadata":{"action":"validate_executable","duration_ms":348.8290309906006},"operation_outcome":"failed","output":"stored as output_bbab28ecefd9","output_ref":"output_bbab28ecefd9","refs":[],"validator_findings":[]},"roles":[],"scope":"environment","tool":"project"}],"envelope_id":"envelope-000011","execution_id":"execution_f80c78c18ef2416f85b506e781c5659b","output_sha256":"e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855","params":{"action":"env","activate":true,"executable":"/usr/bin/gradle","requirement":"[8,)","tool":"gradle"},"result":{"conflicts":[],"error":"Env overlay executable is not executable or does not exist: /usr/bin/gradle","error_code":"ENV_EXECUTABLE_NOT_FOUND","error_tail_preview":"Env overlay executable is not executable or does not exist: /usr/bin/gradle","evidence_assessment":"blocked","evidence_refs":[],"evidence_status":"verified","facts":{},"failure_signature":"ENV_EXECUTABLE_NOT_FOUND:836b0507d4ef8e5a","invocation_status":"completed","metadata":{"action":"validate_executable","duration_ms":348.8290309906006},"operation_outcome":"failed","output":"stored as output_bbab28ecefd9","output_ref":"output_bbab28ecefd9","refs":[],"validator_findings":[]},"roles":[],"scope":"environment","source_attempt_id":"provision-1","source_phase":"provision","tool":"project"},"sequence":12,"source":null,"timestamp":"2026-08-14T11:29:04.583221Z"}
{"event_id":"control-000013","kind":"loop_decision","payload":{"event":{"args":{"action":"env","activate":true,"executable":"/usr/bin/gradle","requirement":"[8,)","tool":"gradle"},"attempt_id":"provision-1","error_code":"ENV_EXECUTABLE_NOT_FOUND","evidence_ref":"output_bbab28ecefd9","failure_signature":"ENV_EXECUTABLE_NOT_FOUND:836b0507d4ef8e5a","invocation_status":"completed","iteration":2,"job_id":"","operation_outcome":"failed","output_cursor":"","phase":"provision","recurrence_count":1,"relevant_scopes":[],"relevant_state":{"artifacts":0,"dependencies":0,"environment":0,"project_analysis":0,"test_runtime":0},"tool_name":"project"},"expected_decision":"continue","expected_reason_code":"new_recurrence_chain"},"sequence":13,"source":null,"timestamp":"2026-08-14T11:29:04.605665Z"}
"""

# Sequences 24-28 — a phase call that emits NO loop_decision, followed by the
# validator observation, the gate that graded it, and the transition it opened.
# One of the silent phase calls the slice campaigns measured.
REAL_SILENT_PHASE_CALL_JSONL = """\
{"event_id":"control-000024","kind":"action_envelope","payload":{"action_fingerprint":"act-102997c616ef519b10af697bf1b39fb2510bafcd6b5f99c89de27a5f5c544f61","envelope_id":"envelope-000024","envelope_sha256":"7d1f80011331e91f96add13d825ed4a57bc4e4fe373e2867436c4e6069c17c25","exact_params":{"action":"done","evidence":["output_bbab28ecefd9"],"key_results":"Checked out apache/kafka.git ref 4.3.1 at commit 26b251a451ce941d3d7a55e6487bcb7f16b5ad48 in /workspace/kafka. Provisioned Java 17 with JAVA_HOME=/usr/lib/jvm/java-17-openjdk-arm64 and verified openjdk version 17.0.19 / javac 17.0.19. Registered and activated Gradle runtime via /workspace/kafka/gradlew; active runtime identities: gradle=/workspace/kafka/gradlew, java=/usr/lib/jvm/java-17-openjdk-arm64/bin/java.","outcome":"success"},"intent_id":"intent-618513fc4f4a","intent_source":"model","tool":"phase","tool_call_id":"call_mgDTaDXN11XdTY1PhDaX7NHD"},"sequence":24,"source":null,"timestamp":"2026-08-14T11:29:15.711998Z"}
{"event_id":"control-000025","kind":"tool_result","payload":{"actual_executions":[{"execution_id":"execution_d8c6f6ed865445b198da307cdac492c4","params":{"action":"done","evidence":["output_bbab28ecefd9"],"key_results":"Checked out apache/kafka.git ref 4.3.1 at commit 26b251a451ce941d3d7a55e6487bcb7f16b5ad48 in /workspace/kafka. Provisioned Java 17 with JAVA_HOME=/usr/lib/jvm/java-17-openjdk-arm64 and verified openjdk version 17.0.19 / javac 17.0.19. Registered and activated Gradle runtime via /workspace/kafka/gradlew; active runtime identities: gradle=/workspace/kafka/gradlew, java=/usr/lib/jvm/java-17-openjdk-arm64/bin/java.","outcome":"success"},"result":{"conflicts":[],"evidence_assessment":"success","evidence_refs":[],"evidence_status":"verified","facts":{"phase":"provision"},"invocation_status":"completed","metadata":{"blocker_owner":"none","control_disposition":"terminal_claimable","duration_ms":125.05102157592773,"gate_result":{"accepted":true,"blocker_owner":"none","claim_disposition":"confirmed","code":"workspace_present","control_disposition":"terminal_claimable","decision_id":"gate-9b1f0f4ef4bcd390c47fa6c892ca4f89","evidence_refs":["/workspace/kafka"],"reason":"workspace /workspace/kafka exists","suggestions":[],"validated_facts":{"provision.workspace_ready":true},"validated_outcome":"success","validator_state":"green"},"phase_claim":{"claimed_outcome":"success","evidence_refs":["output_bbab28ecefd9"],"key_results":"Checked out apache/kafka.git ref 4.3.1 at commit 26b251a451ce941d3d7a55e6487bcb7f16b5ad48 in /workspace/kafka. Provisioned Java 17 with JAVA_HOME=/usr/lib/jvm/java-17-openjdk-arm64 and verified openjdk version 17.0.19 / javac 17.0.19. Registered and activated Gradle runtime via /workspace/kafka/gradlew; active runtime identities: gradle=/workspace/kafka/gradlew, java=/usr/lib/jvm/java-17-openjdk-arm64/bin/java.","phase":"provision","reason":"","signal":"done"},"phase_claim_sha256":"869d31200f4f616c9e7e0ab11732fde890b58b0a0f8912c572b0f6c045ab87b4","phase_signal":"done"},"operation_outcome":"success","output":"output body omitted; verify output_sha256","refs":[],"validator_findings":[]},"roles":[],"scope":"environment","tool":"phase"}],"envelope_id":"envelope-000024","execution_id":"execution_d8c6f6ed865445b198da307cdac492c4","output_sha256":"71be1af1dc38749f9cee20898373d8f8bbec14c89c7692bcb783c6a47e6796b4","params":{"action":"done","evidence":["output_bbab28ecefd9"],"key_results":"Checked out apache/kafka.git ref 4.3.1 at commit 26b251a451ce941d3d7a55e6487bcb7f16b5ad48 in /workspace/kafka. Provisioned Java 17 with JAVA_HOME=/usr/lib/jvm/java-17-openjdk-arm64 and verified openjdk version 17.0.19 / javac 17.0.19. Registered and activated Gradle runtime via /workspace/kafka/gradlew; active runtime identities: gradle=/workspace/kafka/gradlew, java=/usr/lib/jvm/java-17-openjdk-arm64/bin/java.","outcome":"success"},"result":{"conflicts":[],"evidence_assessment":"success","evidence_refs":[],"evidence_status":"verified","facts":{"phase":"provision"},"invocation_status":"completed","metadata":{"blocker_owner":"none","control_disposition":"terminal_claimable","duration_ms":125.05102157592773,"gate_result":{"accepted":true,"blocker_owner":"none","claim_disposition":"confirmed","code":"workspace_present","control_disposition":"terminal_claimable","decision_id":"gate-9b1f0f4ef4bcd390c47fa6c892ca4f89","evidence_refs":["/workspace/kafka"],"reason":"workspace /workspace/kafka exists","suggestions":[],"validated_facts":{"provision.workspace_ready":true},"validated_outcome":"success","validator_state":"green"},"phase_claim":{"claimed_outcome":"success","evidence_refs":["output_bbab28ecefd9"],"key_results":"Checked out apache/kafka.git ref 4.3.1 at commit 26b251a451ce941d3d7a55e6487bcb7f16b5ad48 in /workspace/kafka. Provisioned Java 17 with JAVA_HOME=/usr/lib/jvm/java-17-openjdk-arm64 and verified openjdk version 17.0.19 / javac 17.0.19. Registered and activated Gradle runtime via /workspace/kafka/gradlew; active runtime identities: gradle=/workspace/kafka/gradlew, java=/usr/lib/jvm/java-17-openjdk-arm64/bin/java.","phase":"provision","reason":"","signal":"done"},"phase_claim_sha256":"869d31200f4f616c9e7e0ab11732fde890b58b0a0f8912c572b0f6c045ab87b4","phase_signal":"done"},"operation_outcome":"success","output":"output body omitted; verify output_sha256","refs":[],"validator_findings":[]},"roles":[],"scope":"environment","source_attempt_id":"provision-1","source_phase":"provision","tool":"phase"},"sequence":25,"source":null,"timestamp":"2026-08-14T11:29:15.888668Z"}
{"event_id":"control-000026","kind":"validator_observation","payload":{"blocker_owner":"none","control_disposition":"terminal_claimable","evidence_refs":["/workspace/kafka"],"phase":"provision","reason":"workspace /workspace/kafka exists","validated_facts":{"provision.workspace_ready":true},"validator_state":"green"},"sequence":26,"source":null,"timestamp":"2026-08-14T11:29:16.076369Z"}
{"event_id":"control-000027","kind":"gate_decision","payload":{"blocker_owner":"none","claim_sha256":"869d31200f4f616c9e7e0ab11732fde890b58b0a0f8912c572b0f6c045ab87b4","claimed_outcome":"success","code":"workspace_present","control_disposition":"terminal_claimable","decision_id":"gate-9b1f0f4ef4bcd390c47fa6c892ca4f89","evidence_refs":["/workspace/kafka"],"expected_accepted":true,"expected_outcome":"success","gate_result":{"accepted":true,"blocker_owner":"none","claim_disposition":"confirmed","code":"workspace_present","control_disposition":"terminal_claimable","decision_id":"gate-9b1f0f4ef4bcd390c47fa6c892ca4f89","evidence_refs":["/workspace/kafka"],"reason":"workspace /workspace/kafka exists","suggestions":[],"validated_facts":{"provision.workspace_ready":true},"validated_outcome":"success","validator_state":"green"},"key_results":"Checked out apache/kafka.git ref 4.3.1 at commit 26b251a451ce941d3d7a55e6487bcb7f16b5ad48 in /workspace/kafka. Provisioned Java 17 with JAVA_HOME=/usr/lib/jvm/java-17-openjdk-arm64 and verified openjdk version 17.0.19 / javac 17.0.19. Registered and activated Gradle runtime via /workspace/kafka/gradlew; active runtime identities: gradle=/workspace/kafka/gradlew, java=/usr/lib/jvm/java-17-openjdk-arm64/bin/java.","phase":"provision","reason":"workspace /workspace/kafka exists","signal":"done","source_attempt_id":"provision-1","validated_facts":{"provision.workspace_ready":true},"validator_state":"green"},"sequence":27,"source":null,"timestamp":"2026-08-14T11:29:16.094767Z"}
{"event_id":"control-000028","kind":"phase_transition","payload":{"expected_kind":"advance","expected_reason_code":"workspace_ready","expected_target":"analyze","repair_request":null},"sequence":28,"source":null,"timestamp":"2026-08-14T11:29:16.389095Z"}
"""

# Sequences 84, 86 and 87 of the archived httpcomponents-client session
# (logs/session_20260813_202604_931286_731b3b90c419_2417): a controller-initiated
# forced action, its result, and its decision.
REAL_FORCED_ACTION_JSONL = """\
{"event_id":"control-000084","kind":"forced_action","payload":{"action_fingerprint":"act-ed32c1f2b5a6fcd35676cb04836ad4a07117f45d88709af968b85bcc53acb4a9","action_sha256":"561d7d57bca585ffb2a38dd2532c4a86bc225e709fe5c1158ba869b31fe154cb","candidate_resolution":{"candidates":[],"primary":null,"project_root":"/workspace/httpcomponents-client","status":"unsafe_coordinates","workspace_root":"/workspace"},"candidate_root":"/workspace/httpcomponents-client","candidate_system":null,"envelope_id":"forced-000084","exact_params":{"action":"analyze"},"intent_id":"intent-88f68547fb76","intent_source":"controller","parent_execution_id":null,"phase":"test","policy":"test_attempt_required","reason_code":"unsafe_coordinates","source_attempt_id":"test-1","tool":"project","trigger":"termination_refusal"},"sequence":84,"source":null,"timestamp":"2026-08-14T00:31:50.718911Z"}
{"event_id":"control-000086","kind":"tool_result","payload":{"actual_executions":[{"execution_id":"execution_2d88c950b2e84b3b86414e647ea11a13","params":{"action":"analyze"},"result":{"conflicts":[],"evidence_assessment":"success","evidence_refs":[],"evidence_status":"verified","facts":{},"invocation_status":"completed","metadata":{"analyzer_version":"project-analyzer-v1","build_recommendation":{"build_root":"/workspace/httpcomponents-client","build_system":"maven","test_root":"/workspace/httpcomponents-client","test_system":"maven"},"build_system":"Maven","config_fingerprint":"4109343886 43229 L0","context_updated":true,"dependencies":["org.apache.httpcomponents:httpcomponents-parent","org.apache.httpcomponents.client5:httpclient5-parent","org.apache.httpcomponents.core5:httpcore5","org.apache.httpcomponents.core5:httpcore5-h2","org.apache.httpcomponents.core5:httpcore5-testing","org.apache.httpcomponents.core5:httpcore5-reactive","org.apache.httpcomponents.client5:httpclient5","org.apache.httpcomponents.client5:httpclient5"],"dependencies_total":10,"documentation":{"java_version_requirement":"8","source_path":"README.md"},"duration_ms":5604.017972946167,"existing_files":["pom.xml","README.md"],"existing_files_total":2,"fact_sheet_schema":"sag.project-facts","fact_sheet_version":1,"is_multi_module":true,"java_version":"8","java_version_enforced":false,"java_version_source":"maven-compiler","maven_modules":["httpclient5","httpclient5-observation","httpclient5-fluent","httpclient5-cache","httpclient5-testing"],"maven_modules_total":5,"method_count":1856,"parameterized_info":{"dynamic_tests":0,"parameterized_expansions":27,"parameterized_methods":27,"regular_tests":1807,"repeated_tests":0,"test_factory_methods":0,"test_template_methods":0},"project_path":"/workspace/httpcomponents-client","project_type":"Java","static_test_count":1856,"test_catalog_summary":{"by_module":{"httpclient5":935,"httpclient5-cache":664,"httpclient5-fluent":3,"httpclient5-observation":19,"httpclient5-testing":235},"by_module_total":5,"total_count":1856},"test_count_method":"catalog_based_discovery","test_framework":"JUnit"},"operation_outcome":"success","output":"stored as output_f783c8977587","output_ref":"output_f783c8977587","refs":[],"validator_findings":[]},"roles":[],"scope":"project_analysis","tool":"project"}],"envelope_id":"forced-000084","execution_id":"execution_6cf0ec79ae39447587477b1769961492","output_sha256":"8c3303de6b644ed62e661a1bf3ced3e9f46049b7a135de4fb8227e72161346d5","params":{"action":"analyze"},"result":{"conflicts":["test_candidate_resolution_unresolved:test-1:unsafe_coordinates"],"evidence_assessment":"success","evidence_refs":[],"evidence_status":"verified","facts":{},"invocation_status":"completed","metadata":{"analyzer_version":"project-analyzer-v1","build_recommendation":{"build_root":"/workspace/httpcomponents-client","build_system":"maven","test_root":"/workspace/httpcomponents-client","test_system":"maven"},"build_system":"Maven","config_fingerprint":"4109343886 43229 L0","context_updated":true,"dependencies":["org.apache.httpcomponents:httpcomponents-parent","org.apache.httpcomponents.client5:httpclient5-parent","org.apache.httpcomponents.core5:httpcore5","org.apache.httpcomponents.core5:httpcore5-h2","org.apache.httpcomponents.core5:httpcore5-testing","org.apache.httpcomponents.core5:httpcore5-reactive","org.apache.httpcomponents.client5:httpclient5","org.apache.httpcomponents.client5:httpclient5"],"dependencies_total":10,"documentation":{"java_version_requirement":"8","source_path":"README.md"},"duration_ms":5604.017972946167,"existing_files":["pom.xml","README.md"],"existing_files_total":2,"fact_sheet_schema":"sag.project-facts","fact_sheet_version":1,"is_multi_module":true,"java_version":"8","java_version_enforced":false,"java_version_source":"maven-compiler","maven_modules":["httpclient5","httpclient5-observation","httpclient5-fluent","httpclient5-cache","httpclient5-testing"],"maven_modules_total":5,"method_count":1856,"parameterized_info":{"dynamic_tests":0,"parameterized_expansions":27,"parameterized_methods":27,"regular_tests":1807,"repeated_tests":0,"test_factory_methods":0,"test_template_methods":0},"project_path":"/workspace/httpcomponents-client","project_type":"Java","static_test_count":1856,"test_candidate_refresh_status":"unsafe_coordinates","test_catalog_summary":{"by_module":{"httpclient5":935,"httpclient5-cache":664,"httpclient5-fluent":3,"httpclient5-observation":19,"httpclient5-testing":235},"by_module_total":5,"total_count":1856},"test_count_method":"catalog_based_discovery","test_framework":"JUnit"},"operation_outcome":"success","output":"output body omitted; verify output_sha256","refs":[],"validator_findings":[]},"roles":[],"scope":"project_analysis","source_attempt_id":"test-1","source_phase":"test","tool":"project"},"sequence":86,"source":null,"timestamp":"2026-08-14T00:31:57.406074Z"}
{"event_id":"control-000087","kind":"loop_decision","payload":{"event":{"args":{"action":"analyze"},"attempt_id":"test-1","error_code":"","evidence_ref":"","failure_signature":"","invocation_status":"completed","iteration":13,"job_id":"","operation_outcome":"success","output_cursor":"","phase":"test","recurrence_count":1,"relevant_scopes":[],"relevant_state":{"artifacts":6,"dependencies":0,"environment":6,"project_analysis":8,"test_runtime":0},"tool_name":"project"},"expected_decision":"continue","expected_reason_code":"outcome_not_loop_candidate"},"sequence":87,"source":null,"timestamp":"2026-08-14T00:31:57.423931Z"}
"""


# Sequences 124, 125, 127 and 128 of the archived camel-quarkus d2r3 session
# (logs/session_20260814_093336_763203_12dba3497295_26013): a refused call whose
# `loop_decision` is its only trace, then the retry that answered the refusal —
# same tool, its own envelope, its own result, its own decision. The slice corpus
# (logs/d2r3-serial-20260814/slices/camel-quarkus.md:47,1249) pins seq 124 as one
# of the three refusals with no `tool_result`.
REAL_REFUSED_THEN_RETRY_JSONL = """\
{"event_id":"control-000124","kind":"loop_decision","payload":{"event":{"args":{"action":"provision","java_version":"17"},"attempt_id":"test-1","error_code":"REPAIR_INTENT_REQUIRED","evidence_ref":"output_8a2db03465f6","failure_signature":"REPAIR_INTENT_REQUIRED:111ba7480a39738a","invocation_status":"completed","iteration":17,"job_id":"","operation_outcome":"failed","output_cursor":"","phase":"test","recurrence_count":1,"relevant_scopes":[],"relevant_state":{"artifacts":2,"dependencies":0,"environment":1,"project_analysis":8,"test_runtime":7},"tool_name":"project"},"expected_decision":"continue","expected_reason_code":"new_recurrence_chain"},"sequence":124,"source":null,"timestamp":"2026-08-14T13:56:12.989548Z"}
{"event_id":"control-000125","kind":"action_envelope","payload":{"action_fingerprint":"act-9e03092a3260efbd58df6f98f08f152ce5f85d183a53d23442102aecc1d6d0d8","blocking_fact_refs":["/workspace/camel-quarkus","asm-gate_assessment_303c17e7af6aa2bd-tests_not_executed-4bd3544f"],"domain_id":"test:/workspace","envelope_id":"envelope-000125","envelope_sha256":"60e753fd2fefee761227b84b5fa9a1852700f6a9f7e0479fdcfb1281eac9d4d7","exact_params":{"action":"provision","java_version":"17"},"expected_observation":["job_lifecycle_transition","receipt_assessment","tool_result"],"intent_id":"intent-f19aec72dbd1","intent_source":"model","next_action_kind":"provision","repair_context_id":"rcx-b41b0c9c98cd","repair_context_sha256":"c84b49273a784b202e98f5db59b5674c690cba905bca10d2142be1b05b16415f","repair_hypothesis":"The test run was blocked because the active runtime was Java 11 while the project\u2019s formatter plugin requires Java 17. Provisioning JDK 17 should satisfy the plugin prerequisite and allow the Maven wrapper test goal to start and produce runner receipts.","stop_condition":"Stop once Java 17 is provisioned and the next test dispatch can be attempted with the wrapper.","tool":"project","tool_call_id":"call_VWISTioI2BXdf64WnF7mRqsB","trigger_assessment_id":"asm-gate_assessment_303c17e7af6aa2bd-tests_not_executed-4bd3544f"},"sequence":125,"source":null,"timestamp":"2026-08-14T13:56:16.047479Z"}
{"event_id":"control-000127","kind":"tool_result","payload":{"actual_executions":[{"execution_id":"execution_2758f01a8bf9449b8c3a6e06c727d2ea","params":{"action":"provision","java_version":"17"},"result":{"conflicts":[],"evidence_assessment":"success","evidence_refs":[],"evidence_status":"verified","facts":{},"invocation_status":"completed","metadata":{"architecture":"arm64","duration_ms":2992.069959640503,"java_home":"/usr/lib/jvm/java-17-openjdk-arm64","java_version":"17","package":"openjdk-17-jdk"},"operation_outcome":"success","output":"stored as output_8351840c096f","output_ref":"output_8351840c096f","refs":[],"validator_findings":[]},"roles":[],"scope":"environment","tool":"project"}],"envelope_id":"envelope-000125","execution_id":"execution_2758f01a8bf9449b8c3a6e06c727d2ea","output_sha256":"b334750081117ddb2fe8cf5a00a8f0fa3aff049286e9c2c7ccd6d0d796339c2f","params":{"action":"provision","java_version":"17"},"result":{"conflicts":[],"evidence_assessment":"success","evidence_refs":[],"evidence_status":"verified","facts":{},"invocation_status":"completed","metadata":{"architecture":"arm64","duration_ms":2992.069959640503,"java_home":"/usr/lib/jvm/java-17-openjdk-arm64","java_version":"17","package":"openjdk-17-jdk"},"operation_outcome":"success","output":"stored as output_8351840c096f","output_ref":"output_8351840c096f","refs":[],"validator_findings":[]},"roles":[],"scope":"environment","source_attempt_id":"test-1","source_phase":"test","tool":"project"},"sequence":127,"source":null,"timestamp":"2026-08-14T13:56:19.830252Z"}
{"event_id":"control-000128","kind":"loop_decision","payload":{"event":{"args":{"action":"provision","java_version":"17"},"attempt_id":"test-1","error_code":"","evidence_ref":"output_8351840c096f","failure_signature":"","invocation_status":"completed","iteration":18,"job_id":"","operation_outcome":"success","output_cursor":"","phase":"test","recurrence_count":1,"relevant_scopes":[],"relevant_state":{"artifacts":2,"dependencies":0,"environment":1,"project_analysis":8,"test_runtime":7},"tool_name":"project"},"expected_decision":"continue","expected_reason_code":"outcome_not_loop_candidate"},"sequence":128,"source":null,"timestamp":"2026-08-14T13:56:19.849063Z"}
"""


# Sequences 116-160 of the kafka d2r3 session, in LEDGER order: the transition
# into build, an advisor call made inside build, that call's result, the
# transition into test, an advisor call made inside test, and the evidence_close
# that ends the run's last banded phase. The fence below feeds them in a
# different order — the result late — because that is the arrival order a slow
# call produces and the one the run pointer has to survive.
REAL_BUILD_TO_TEST_JSONL = """\
{"event_id":"control-000116","kind":"phase_transition","payload":{"expected_kind":"advance","expected_reason_code":"analysis_ready","expected_target":"build","repair_request":null},"sequence":116,"source":null,"timestamp":"2026-08-14T11:30:09.629860Z"}
{"event_id":"control-000117","kind":"action_envelope","payload":{"envelope_id":"envelope-000117","envelope_sha256":"d5e535e6ddc7bcb19b1deb321262684cd2941ab851bd687af28cc08bfff05455","exact_params":{},"tool":"advisor","tool_call_id":"advisor-entry-1"},"sequence":117,"source":null,"timestamp":"2026-08-14T11:30:13.900287Z"}
{"event_id":"control-000118","kind":"tool_result","payload":{"actual_executions":[],"envelope_id":"envelope-000117","execution_id":"execution_42eedea8469644d8b7821a5252952c3d","output_sha256":"68088b458619e9f0383bb986dc96877ee76e97e7ecc17149d47031c4844522e4","params":{},"result":{"conflicts":[],"evidence_assessment":"success","evidence_refs":[],"evidence_status":"verified","facts":{},"invocation_status":"completed","metadata":{"advisor":"advice","advisor_call_index":1,"advisor_model":"gpt-5.4-mini"},"operation_outcome":"success","output":"stored as output_2c89f3a1127b","output_ref":"output_2c89f3a1127b","refs":[],"validator_findings":[]},"roles":[],"scope":"artifacts","source_attempt_id":"build-1","source_phase":"build","tool":"advisor"},"sequence":118,"source":null,"timestamp":"2026-08-14T11:30:13.922815Z"}
{"event_id":"control-000138","kind":"phase_transition","payload":{"expected_kind":"advance","expected_reason_code":"test_entry_ready","expected_target":"test","repair_request":null},"sequence":138,"source":null,"timestamp":"2026-08-14T11:32:31.686985Z"}
{"event_id":"control-000139","kind":"action_envelope","payload":{"envelope_id":"envelope-000139","envelope_sha256":"813ac72fccadcdbaa5e25e5c4dd93bd591af8aa4d2d5ce5575efdae5674563ac","exact_params":{},"tool":"advisor","tool_call_id":"advisor-entry-2"},"sequence":139,"source":null,"timestamp":"2026-08-14T11:32:36.541626Z"}
{"event_id":"control-000160","kind":"phase_transition","payload":{"expected_kind":"evidence_close","expected_reason_code":"test_terminal","expected_target":null,"repair_request":null},"sequence":160,"source":null,"timestamp":"2026-08-14T11:40:43.104247Z"}
"""


# Sequences 84, 87 and 90 of the archived rocketmq-externals run
# (logs/session_20260813_203227_051092_171f42548985_2604): one `phase` call, and
# TWO gate decisions for the same `build` phase landing on the turn it opened.
# Neither grading carries a `decision_id` and neither names a `supersedes`, so
# the second one is not a chain — it is the ledger changing its mind (accepted
# false -> true) with nothing in the record connecting the two words.
REAL_SILENT_GATE_REPLACEMENT_JSONL = """\
{"event_id":"control-000084","kind":"action_envelope","payload":{"action_fingerprint":"act-8dce3dd6e3276294945acb4f313a3814226c98a3fbb62eb05376cd22ecc8d9a1","envelope_id":"envelope-000084","envelope_sha256":"9effe6fcfa1cce98a7a35a84e334bfda3059cc1ee2ed631fe8ed021f8ca533be","exact_params":{"action":"blocked","evidence":["output_2ba10334c186","output_531e62e1640f","output_227e4c9296d5","output_f71a16515207","output_e90f4d1d80e8"],"outcome":"unknown","reason":"Maven executable could not be located in the container filesystem search scope, and the build facade previously refused dispatch because no valid registered runtime was available. Without a discoverable mvn binary, I cannot produce the required terminal build attempt receipt for this phase."},"intent_id":"intent-748afcc92ca9","intent_source":"model","tool":"phase","tool_call_id":"call_kXNfRcjHjCHS2LJGOWeBsdZa"},"sequence":84,"source":null,"timestamp":"2026-08-14T00:34:38.737018Z"}
{"event_id":"control-000087","kind":"gate_decision","payload":{"blocker_owner":"harness","claimed_outcome":"unknown","code":"repair_assessment_persist_failed","control_disposition":"harness_recovery_required","evidence_refs":["output_2ba10334c186","output_531e62e1640f","output_227e4c9296d5","output_f71a16515207","output_e90f4d1d80e8"],"expected_accepted":false,"expected_outcome":"unknown","key_results":"","phase":"build","reason":"Build phase has no terminal build attempt receipt; no project outcome can be claimed yet.; controller could not persist the judge-owned repair context (repair_assessment_persist_failed)","signal":"blocked","source_attempt_id":"build-1","validated_facts":{"build_attempt_requirement":{"manifest_status":"unavailable","reason_code":"build_attempt_missing","receipt_binding_status":"project_root_missing","terminal_build_receipts":0}},"validator_state":"unavailable"},"sequence":87,"source":null,"timestamp":"2026-08-14T00:34:39.044425Z"}
{"event_id":"control-000090","kind":"gate_decision","payload":{"blocker_owner":"unknown","claimed_outcome":"unknown","code":"repair_assessment_persist_failed","control_disposition":"terminal_claimable","evidence_refs":["output_2ba10334c186","output_531e62e1640f","output_227e4c9296d5","output_f71a16515207","output_e90f4d1d80e8"],"expected_accepted":true,"expected_outcome":"unknown","key_results":"","phase":"build","reason":"repair_assessment_persist_failed; Build phase has no terminal build attempt receipt; no project outcome can be claimed yet.; controller could not persist the judge-owned repair context (repair_assessment_persist_failed)","signal":"blocked","source_attempt_id":"build-1","validated_facts":{"build_attempt_requirement":{"manifest_status":"unavailable","reason_code":"build_attempt_missing","receipt_binding_status":"project_root_missing","terminal_build_receipts":0}},"validator_state":"unavailable"},"sequence":90,"source":null,"timestamp":"2026-08-14T00:34:39.224468Z"}
"""

#: SYNTHETIC. `gate_outcome_revised` appears in no archived ledger, so its bytes
#: are built from `GateOutcomeRevisedPayload` — every field it declares, and the
#: two-distinct-gradings rule its validator enforces. The word it revises is
#: kafka's real provision gate `gate-9b1f0f4ef4bcd390c47fa6c892ca4f89`, so the
#: fixture chains onto archived bytes rather than onto more invention.
SYNTHETIC_GATE_REVISION_JSONL = (
    '{"event_id":"control-000029","kind":"gate_outcome_revised","payload":'
    '{"phase":"provision","delivered_decision_id":"gate-9b1f0f4ef4bcd390c47fa6c892ca4f89",'
    '"revised_decision_id":"gate-e2a70c5b6d1f4a8390b4c7d2e5f60189",'
    '"delivered_outcome":"success","revised_outcome":"partial",'
    '"delivered_accepted":true,"revised_accepted":false,'
    '"reason":"the workspace check read a mount that had been replaced",'
    '"code":"workspace_stale","observation_text":"Gate outcome revised for provision: '
    'success -> partial","source_attempt_id":"provision-1"},'
    '"sequence":29,"source":null,"timestamp":"2026-08-14T11:29:17.010000Z"}'
)

#: SYNTHETIC. The third link: a `gate_decision` that names the revision in its
#: `supersedes`, which is the chain `GateDecisionPayload.supersedes` documents.
SYNTHETIC_GATE_AFTER_REVISION_JSONL = (
    '{"event_id":"control-000030","kind":"gate_decision","payload":'
    '{"claimed_outcome":"partial","code":"workspace_present",'
    '"decision_id":"gate-77c1d4e9a05b46f2ac83be91d6407f52","evidence_refs":["/workspace/kafka"],'
    '"expected_accepted":true,"expected_outcome":"success","key_results":"",'
    '"phase":"provision","reason":"workspace re-verified after the revision",'
    '"signal":"done","source_attempt_id":"provision-1",'
    '"supersedes":"gate-e2a70c5b6d1f4a8390b4c7d2e5f60189",'
    '"validated_facts":{"provision.workspace_ready":true},"validator_state":"green"},'
    '"sequence":30,"source":null,"timestamp":"2026-08-14T11:29:17.400000Z"}'
)

#: SYNTHETIC. A revision naming a grading no gate in this run ever delivered —
#: the shape `sag.agent.replay` rejects outright ("gate_outcome_revised names a
#: word that was never delivered"). Replay may abort on it; a read-only
#: derivation may not, so this layer states it instead.
SYNTHETIC_ORPHAN_REVISION_JSONL = (
    '{"event_id":"control-000031","kind":"gate_outcome_revised","payload":'
    '{"phase":"provision","delivered_decision_id":"gate-000000000000000000000000000000ff",'
    '"revised_decision_id":"gate-e2a70c5b6d1f4a8390b4c7d2e5f60189",'
    '"delivered_outcome":"success","revised_outcome":"partial",'
    '"delivered_accepted":true,"revised_accepted":false,"reason":"",'
    '"code":"workspace_stale","observation_text":"Gate outcome revised for provision: '
    'success -> partial","source_attempt_id":"provision-1"},'
    '"sequence":31,"source":null,"timestamp":"2026-08-14T11:29:18.010000Z"}'
)

#: SYNTHETIC and DEGRADED — a gate event carrying no word at all. No engine
#: writes this (both payload classes require the outcome); it stands for a
#: payload this schema version cannot read whole, which is exactly the input
#: the "warnings, never exceptions" rule of spec §3 exists for.
SYNTHETIC_WORDLESS_GATE_JSONL = (
    '{"event_id":"control-000032","kind":"gate_decision","payload":'
    '{"phase":"provision","decision_id":"gate-77c1d4e9a05b46f2ac83be91d6407f52"},'
    '"sequence":32,"source":null,"timestamp":"2026-08-14T11:29:19.010000Z"}'
)
SYNTHETIC_WORDLESS_REVISION_JSONL = (
    '{"event_id":"control-000033","kind":"gate_outcome_revised","payload":'
    '{"phase":"provision","delivered_decision_id":"gate-9b1f0f4ef4bcd390c47fa6c892ca4f89",'
    '"revised_decision_id":"gate-e2a70c5b6d1f4a8390b4c7d2e5f60189"},'
    '"sequence":33,"source":null,"timestamp":"2026-08-14T11:29:19.410000Z"}'
)

#: SYNTHETIC. No archived session seals a `turn_record` — the kind landed with
#: Stage B — so its bytes are built from `TurnRecordPayload`, and the fences
#: below validate them against that class. The turn it seals is kafka's real
#: clone call at seq 3, so the record chains onto archived bytes.
SYNTHETIC_TURN_RECORD_JSONL = (
    '{"event_id":"control-000007","kind":"turn_record","payload":'
    '{"turn_id":1,"phase":"provision","iteration":1,"actor":"model",'
    '"window_digest":{"system_prompt_sha256":"' + "a" * 64 + '",'
    '"component_refs":["output_aaaaaaaaaaaa","output_bbbbbbbbbbbb","output_cccccccccccc"]},'
    '"envelope_ref":"envelope-000003","observation_ref":"output_dddddddddddd",'
    '"tokens_in":4134,"tokens_out":71,'
    '"t0":"2026-08-14T11:28:36.915154Z","t1":"2026-08-14T11:28:49.760291Z"},'
    '"sequence":7,"source":null,"timestamp":"2026-08-14T11:28:49.900000Z"}'
)

#: SYNTHETIC. The same record for a window the engine had to CUT: the first slot
#: names how many older components it could not name (spec §7 amendment 6), and
#: the rest are the newest ones that fit, in render order.
SYNTHETIC_TRUNCATED_TURN_RECORD_JSONL = SYNTHETIC_TURN_RECORD_JSONL.replace(
    '"component_refs":["output_aaaaaaaaaaaa"',
    '"component_refs":["window_truncated:953","output_aaaaaaaaaaaa"',
)

#: SYNTHETIC, in field names only: the keys are the ones camel-quarkus seq 122
#: actually wrote, with the bounded context trimmed to what this layer reads.
SYNTHETIC_REPAIR_CONTEXT_JSONL = (
    '{"event_id":"control-000122","kind":"repair_context_opened","payload":'
    '{"context":{"blocker_owner":"project","domain_id":"test:/workspace",'
    '"repair_context_id":"rcx-b41b0c9c98cd","trigger_assessment_id":'
    '"asm-gate_assessment_303c17e7af6aa2bd-tests_not_executed-4bd3544f"},'
    '"context_sha256":"c84b49273a784b202e98f5db59b5674c690cba905bca10d2142be1b05b16415f",'
    '"source_gate_sequence":121,"source_phase_attempt_id":"test-1"},'
    '"sequence":122,"source":null,"timestamp":"2026-08-14T13:56:10.000000Z"}'
)


def _lines(block: str) -> list[str]:
    return block.strip().splitlines()


REAL_TRIPLE = _lines(REAL_TRIPLE_JSONL)


def test_a_decision_envelope_result_triple_becomes_one_closed_turn():
    r = TrajectoryReducer()
    for line in REAL_TRIPLE:  # verbatim from the kafka session
        r.feed(line)
    snap = r.snapshot()
    assert len(snap.turns) == 1
    t = snap.turns[0]
    assert t.actor == "model" and t.call is not None and t.observation is not None
    assert snap.warnings == []


def test_the_triple_joins_on_the_field_names_the_engine_actually_wrote():
    r = TrajectoryReducer()
    for line in REAL_TRIPLE:
        r.feed(line)
    t = r.snapshot().turns[0]
    assert t.turn_id == 1
    assert t.call.tool == "project"
    assert t.call.params_ref == "envelope-000003"  # payload.envelope_id
    assert t.observation.ref == "output_6163859b019d"  # result.output_ref
    assert t.observation.error_code is None and t.observation.failure_signature is None
    assert t.phase == "provision"  # tool_result.source_phase, confirmed by the decision
    assert t.iteration == 1  # loop_decision.event.iteration
    assert t.control_seq == [3, 4, 6]  # every event folded into this turn
    assert t.t0 == "2026-08-14T11:28:36.915154Z"  # the envelope opened it
    assert t.t1 == "2026-08-14T11:28:49.545505Z"  # the result closed it


def test_an_envelope_never_gets_stapled_onto_a_decision_that_precedes_it():
    """A decision with no envelope behind it is a refusal of an EARLIER call.

    The engine writes envelope→result→decision (see `REAL_TRIPLE`), so a
    decision arriving first is never "this envelope, early" — it is a call the
    ledger never enveloped. Adopting the next envelope into it would assert that
    that envelope returned the refusal, which is exactly the archaeology this
    layer exists to end (spec §0). Each keeps its own row, and each says what it
    is missing.
    """
    envelope, result, decision = REAL_TRIPLE
    r = TrajectoryReducer()
    for line in (decision, envelope, result):
        r.feed(line)
    snap = r.snapshot()
    assert len(snap.turns) == 2
    orphan, call = snap.turns
    assert orphan.call is None and orphan.control_seq == [6]
    assert call.call.params_ref == "envelope-000003" and call.control_seq == [3, 4]
    assert {(w.code, w.control_seq) for w in snap.warnings} == {
        ("missing_envelope", 6),
        ("missing_tool_result", 6),
        ("missing_loop_decision", 3),
        # And the arithmetic agrees with the pairing: one decision was made
        # about a call this ledger neither enveloped nor answered.
        ("conservation_violation", None),
    }


def test_a_refused_call_does_not_swallow_the_retry_that_followed_it():
    """camel-quarkus seq 124: the refusal is its own row, the retry is its own.

    Both name the `project` tool, and the retry is the model answering the
    refusal — which is precisely when a same-tool adoption rule would fuse them
    and report that `envelope-000125` returned `REPAIR_INTENT_REQUIRED`. The
    ledger says `envelope-000125` succeeded.
    """
    r = TrajectoryReducer()
    for line in _lines(REAL_REFUSED_THEN_RETRY_JSONL):
        r.feed(line)
    snap = r.snapshot()
    assert len(snap.turns) == 2

    refusal, retry = snap.turns
    assert refusal.call is None  # seq 124 has no envelope at all
    assert refusal.iteration == 17 and refusal.control_seq == [124]
    assert refusal.observation.error_code == "REPAIR_INTENT_REQUIRED"
    assert refusal.observation.ref == "output_8a2db03465f6"

    assert retry.call.tool == "project" and retry.call.params_ref == "envelope-000125"
    assert retry.iteration == 18 and retry.control_seq == [125, 127, 128]
    assert retry.observation.ref == "output_8351840c096f"  # what the retry actually returned
    assert retry.observation.error_code is None  # tool_result says operation_outcome=success

    assert {(w.code, w.control_seq) for w in snap.warnings} == {
        ("missing_envelope", 124),
        ("missing_tool_result", 124),
        # seq 124 is a decision about a call with no envelope and no result:
        # the two sides that count calls come up one short of the decisions.
        ("conservation_violation", None),
    }


def test_a_decision_with_no_result_states_its_holes_the_moment_it_opens():
    """A hole is a statement about the ledger as it stands, not as it ends.

    Waiting for the next turn to open is what made the LAST open turn's holes
    unsayable in a live follow — the run's most interesting turn is exactly the
    one still running. So the claim is made immediately and withdrawn if the
    missing piece arrives.
    """
    first, second = _lines(REAL_TWO_DECISIONS_JSONL)
    r = TrajectoryReducer()
    opened = r.feed(first)
    assert {(w.code, w.control_seq, w.turn_id) for w in opened.warnings} == {
        ("missing_envelope", 6, 1),
        ("missing_tool_result", 6, 1),
    }
    sealed = r.feed(second)
    assert {(w.code, w.turn_id) for w in sealed.warnings} == {
        ("missing_envelope", 2),
        ("missing_tool_result", 2),
        # Turn 1 stopped being the call in flight when turn 2 opened, so the
        # count fence can finally say what its holes already said.
        ("conservation_violation", None),
    }
    assert sealed.retracted_warnings == []  # turn 1's holes are still holes
    assert len(r.snapshot().turns) == 2


def test_a_hole_that_fills_is_retracted_and_never_survives_in_the_accumulation():
    """The delta protocol: a statement is added when true, withdrawn when not."""
    envelope, result, decision = REAL_TRIPLE
    r = TrajectoryReducer()

    opened = r.feed(envelope)
    assert {w.code for w in opened.warnings} == {"missing_tool_result", "missing_loop_decision"}
    assert opened.retracted_warnings == []

    answered = r.feed(result)
    assert answered.warnings == []
    assert {w.code for w in answered.retracted_warnings} == {"missing_tool_result"}

    decided = r.feed(decision)
    assert {w.code for w in decided.retracted_warnings} == {"missing_loop_decision"}
    assert r.snapshot().warnings == []


def test_an_unknown_kind_is_a_warning_never_an_exception():
    r = TrajectoryReducer()
    delta = r.feed('{"sequence": 999, "kind": "totally_new_event", "payload": {}}')
    assert delta.warnings and delta.warnings[0].code == "unknown_event_kind"


def test_a_malformed_line_is_a_warning_never_an_exception():
    r = TrajectoryReducer()
    assert r.feed("{not json at all").warnings[0].code == "malformed_event_line"
    assert r.feed('["a list is not an event"]').warnings[0].code == "malformed_event_line"
    assert r.feed("   ").warnings == []  # a blank line is not a hole
    assert r.snapshot().turns == []


def test_a_failed_call_carries_its_error_code_and_failure_signature():
    r = TrajectoryReducer()
    for line in _lines(REAL_FAILED_CALL_JSONL):
        r.feed(line)
    t = r.snapshot().turns[0]
    assert t.observation.error_code == "ENV_EXECUTABLE_NOT_FOUND"
    assert t.observation.failure_signature == "ENV_EXECUTABLE_NOT_FOUND:836b0507d4ef8e5a"
    assert t.iteration == 2 and t.phase == "provision"


def test_a_silent_phase_call_seals_a_missing_loop_decision_warning():
    """The ten silent phase calls of the campaigns are stated, not swallowed."""
    r = TrajectoryReducer()
    for line in _lines(REAL_SILENT_PHASE_CALL_JSONL):
        r.feed(line)
    snap = r.snapshot()
    assert len(snap.turns) == 1
    assert snap.turns[0].call.tool == "phase" and snap.turns[0].observation is not None
    assert [w.code for w in snap.warnings] == ["missing_loop_decision"]
    assert snap.warnings[0].control_seq == 24


def test_the_gate_word_attaches_to_the_turn_that_carried_it():
    r = TrajectoryReducer()
    for line in _lines(REAL_SILENT_PHASE_CALL_JSONL):
        r.feed(line)
    snap = r.snapshot()
    gate = snap.turns[0].gate
    assert gate.word == "success"  # the outcome the gate delivered, not the claim
    assert gate.decision_id == "gate-9b1f0f4ef4bcd390c47fa6c892ca4f89"
    assert gate.supersedes is None
    assert 27 in snap.turns[0].control_seq


def test_a_phase_transition_bands_the_phases():
    r = TrajectoryReducer()
    for line in _lines(REAL_SILENT_PHASE_CALL_JSONL):
        r.feed(line)
    phases = r.snapshot().phases
    assert [p.name for p in phases] == ["provision", "analyze"]
    assert phases[0].termination == "advance"
    assert [g.decision_id for g in phases[0].gates] == ["gate-9b1f0f4ef4bcd390c47fa6c892ca4f89"]
    assert phases[1].termination is None and phases[1].gates == []


def test_a_phase_named_again_after_it_closed_reuses_its_band():
    """Only a transition opens a segment; a stray later mention joins the old one.

    After `provision` closes, the ledger keeps naming it — a late `loop_decision`
    for a call made before the transition, for instance. Appending a second
    `provision` band for that would put the run in two places at once and hand
    the timeline a phantom segment to draw. Bands are contiguous segments of the
    run, so the late mention attaches to the segment that already exists.
    """
    r = TrajectoryReducer()
    for line in _lines(REAL_SILENT_PHASE_CALL_JSONL):  # ends by transitioning to analyze
        r.feed(line)
    r.feed(REAL_TRIPLE[2])  # a loop_decision naming phase "provision", after it closed

    phases = r.snapshot().phases
    assert [p.name for p in phases] == ["provision", "analyze"]
    assert phases[0].termination == "advance"  # the late mention does not un-close it


def test_a_transition_closes_the_phase_the_run_is_in_not_the_last_band_appended():
    """A gate naming a phase the run has not reached must not steal the closure.

    `gate_decision` bands the phase it graded, which can be a phase no turn has
    entered. When the closing transition then arrived, terminating "whatever
    band was appended last" wrote the termination onto that stranger and left
    the phase actually being left open forever.
    """
    stray_gate = _lines(REAL_SILENT_PHASE_CALL_JSONL)[3].replace(
        '"phase":"provision"', '"phase":"build"'
    )
    r = TrajectoryReducer()
    for line in REAL_TRIPLE:  # the run is in provision
        r.feed(line)
    r.feed(stray_gate)  # a gate grading "build", which nothing has entered
    r.feed(_lines(REAL_SILENT_PHASE_CALL_JSONL)[4])  # advance -> analyze

    bands = {p.name: p.termination for p in r.snapshot().phases}
    assert bands["provision"] == "advance"  # the phase the run was actually in
    assert bands["build"] is None  # the stranger keeps its own (absent) ending


def test_a_late_result_bands_its_own_turn_without_moving_the_run_backwards():
    """Where the run IS is a transition's word; a turn's phase is its own event's.

    A `tool_result` names the phase its CALL was made in, and that call can have
    been made before the run left the phase — an advisor or build call whose
    answer lands after the transition. Letting that late word move the run
    pointer put the run back in `build` while it was executing `test`, so the
    next transition closed the band it had already closed and `test` was left
    open for the rest of the run. Two things must be true at once: the late
    result bands ITS turn `build`, and the run is still in `test`.
    """
    into_build, call, answer, into_test, later_call, close = _lines(REAL_BUILD_TO_TEST_JSONL)
    r = TrajectoryReducer()
    for line in (into_build, call, into_test, answer, later_call, close):
        r.feed(line)

    snap = r.snapshot()
    assert [t.call.params_ref for t in snap.turns] == ["envelope-000117", "envelope-000139"]
    assert snap.turns[0].phase == "build"  # the late result banded its own turn
    assert snap.turns[1].phase == "test"  # a turn opened after it is where the run is
    assert {p.name: p.termination for p in snap.phases} == {
        "build": "advance",
        "test": "evidence_close",  # the band the run was actually in when it closed
    }


def test_the_run_pointer_is_seeded_by_the_phase_no_transition_ever_names():
    """A run's opening phase is entered by starting, not by a transition.

    Nothing announces `provision`: the first transition in every archived ledger
    already names the phase being entered NEXT. So the pointer is seeded by the
    first band that comes into existence while the run has not been placed —
    which is not the pointer moving, it is the pointer being set — and that seed
    is what lets `provision` be terminated by the transition that leaves it.
    """
    r = TrajectoryReducer()
    for line in _lines(REAL_SILENT_PHASE_CALL_JSONL):  # provision, then advance -> analyze
        r.feed(line)
    assert {p.name: p.termination for p in r.snapshot().phases} == {
        "provision": "advance",
        "analyze": None,
    }


def test_a_naive_timestamp_is_read_as_utc_instead_of_raising():
    """Ledgers mix aware and naive stamps; a wall clock is not allowed to crash."""
    bound = (
        '{"event_id":"control-000001","kind":"evidence_store_bound","payload":'
        '{"run_id":"r-1","store_identity":"docker:abc"},"sequence":1,'
        '"source":null,"timestamp":"%s"}'
    )
    r = TrajectoryReducer()
    r.feed(bound % "2026-08-14T11:28:33.753666")  # naive
    r.feed(bound % "2026-08-14T11:29:33.753666Z")  # aware
    assert r.snapshot().session.wall_clock_seconds == 60.0


def test_a_forced_action_opens_a_controller_turn():
    r = TrajectoryReducer()
    for line in _lines(REAL_FORCED_ACTION_JSONL):
        r.feed(line)
    snap = r.snapshot()
    assert len(snap.turns) == 1
    t = snap.turns[0]
    assert t.actor == "controller"
    assert t.call.tool == "project" and t.call.params_ref == "forced-000084"
    assert t.observation is not None and t.phase == "test"
    assert snap.warnings == []
    forced = [a for a in snap.annotations if a.kind == "forced"]
    assert len(forced) == 1 and forced[0].turn_id == 1
    assert forced[0].data["trigger"] == "termination_refusal"


def test_a_known_kind_the_reducer_derives_nothing_from_is_not_a_warning():
    """`validator_observation` is in the ledger's vocabulary — so it is not a hole."""
    r = TrajectoryReducer()
    validator_observation = _lines(REAL_SILENT_PHASE_CALL_JSONL)[2]
    assert '"kind": "validator_observation"' in validator_observation.replace('":"', '": "')
    assert r.feed(validator_observation).warnings == []


def test_the_store_binding_names_the_run():
    r = TrajectoryReducer()
    delta = r.feed(
        '{"event_id":"control-000001","kind":"evidence_store_bound","payload":'
        '{"run_id":"r-1","store_identity":"docker:abc"},"sequence":1,'
        '"source":null,"timestamp":"2026-08-14T11:28:33.753666Z"}'
    )
    assert delta.warnings == []
    assert delta.session_patch == {"run_id": "r-1", "wall_clock_seconds": 0.0}
    assert r.snapshot().session.run_id == "r-1"


def test_accumulated_deltas_reproduce_the_snapshot_exactly():
    """The delta stream is the snapshot, told one event at a time.

    Not "the same turns" — the SAME DOCUMENT: turns, phases, annotations,
    session, and the warnings, which are the part that used to drift because a
    turn's holes only shipped when the next turn opened.
    """
    r = TrajectoryReducer()
    accumulator = DeltaAccumulator()
    for block in (REAL_TRIPLE_JSONL, REAL_FAILED_CALL_JSONL, REAL_SILENT_PHASE_CALL_JSONL):
        for line in _lines(block):
            accumulator.feed(r.feed(line))
    assert accumulator.snapshot() == r.snapshot()


def test_the_last_open_turn_ships_its_holes_like_every_other_turn():
    """The turn still running is the one a live watcher most needs stated."""
    r = TrajectoryReducer()
    accumulator = DeltaAccumulator()
    for line in _lines(REAL_TRIPLE_JSONL) + [_lines(REAL_FAILED_CALL_JSONL)[0]]:
        accumulator.feed(r.feed(line))
    assert [(w.code, w.turn_id) for w in accumulator.snapshot().warnings] == [
        ("missing_loop_decision", 2),
        ("missing_tool_result", 2),
    ]
    assert accumulator.snapshot() == r.snapshot()


def _walk(gates, start):
    """Follow `supersedes` back from one gate, gathering the chain it names."""
    by_id = {gate.decision_id: gate for gate in gates if gate.decision_id}
    chain = [start]
    while chain[-1].supersedes is not None:
        previous = by_id.get(chain[-1].supersedes)
        assert previous is not None, f"the chain breaks at {chain[-1].supersedes}"
        chain.append(previous)
    return chain


def test_the_synthetic_gate_lines_are_the_shape_the_engine_would_seal():
    """A fixture the engine could never write proves nothing about the engine.

    `gate_outcome_revised` is emitted by no archived run, so its fences are
    built rather than lifted. That is only honest while the built bytes still
    validate as the payload `react_engine._emit_gate_outcome_revised` seals —
    including the rule that a revision must name two distinct gradings and a
    word that actually moved.
    """
    for line in (SYNTHETIC_GATE_REVISION_JSONL, SYNTHETIC_ORPHAN_REVISION_JSONL):
        GateOutcomeRevisedPayload.model_validate(json.loads(line)["payload"])
    GateDecisionPayload.model_validate(json.loads(SYNTHETIC_GATE_AFTER_REVISION_JSONL)["payload"])


def test_a_second_gate_that_supersedes_nothing_replaces_the_first_out_loud():
    """rocketmq-externals seq 87 then 90: two words, one turn, no chain.

    Both gradings name phase `build`, land on the turn `envelope-000084`
    opened, and disagree — `expected_accepted` false, then true. Neither
    carries a `decision_id` and neither names a `supersedes`, so nothing in the
    record connects them. Holding only the last one made the trajectory report
    a run that was graded once, which the ledger's own bytes contradict; the
    turn keeps the word that was delivered last AND the replacement is stated,
    because both are facts.
    """
    r = TrajectoryReducer()
    for line in _lines(REAL_SILENT_GATE_REPLACEMENT_JSONL):
        r.feed(line)
    snap = r.snapshot()

    assert len(snap.turns) == 1
    turn = snap.turns[0]
    assert turn.gate.word == "unknown" and turn.gate.decision_id is None
    assert turn.control_seq == [84, 87, 90]

    replaced = [w for w in snap.warnings if w.code == "gate_replaced_without_supersedes"]
    assert len(replaced) == 1
    assert replaced[0].control_seq == 90  # the event that did the replacing
    assert replaced[0].detail == (
        "turn 1 held gate None and a second gate None replaced it "
        "without naming it in supersedes"
    )


def test_a_gate_that_names_the_word_it_supersedes_replaces_it_in_silence():
    """The designed chain is not a defect, so it does not get a warning."""
    r = TrajectoryReducer()
    for line in _lines(REAL_SILENT_PHASE_CALL_JSONL)[:4]:  # the call, its result, its gate
        r.feed(line)
    r.feed(SYNTHETIC_GATE_REVISION_JSONL)
    r.feed(SYNTHETIC_GATE_AFTER_REVISION_JSONL)
    snap = r.snapshot()

    assert [w for w in snap.warnings if w.code == "gate_replaced_without_supersedes"] == []
    assert snap.turns[0].gate.decision_id == "gate-77c1d4e9a05b46f2ac83be91d6407f52"


def test_a_revision_attaches_to_the_turn_that_carried_the_word_it_revises():
    """A revision revises a delivered word, not whichever turn happens to be open.

    By the time the engine may revise an outcome, the model has acted on the
    first word and the run has moved on — the open turn is a later call that
    was never graded. Stapling the revision there would put a gate on a turn
    that never had one and leave the turn that WAS graded showing a word the
    record has already replaced.
    """
    r = TrajectoryReducer()
    for line in _lines(REAL_SILENT_PHASE_CALL_JSONL)[:4]:
        r.feed(line)
    r.feed(_lines(REAL_FAILED_CALL_JSONL)[0])  # a later call opens turn 2
    delta = r.feed(SYNTHETIC_GATE_REVISION_JSONL)
    snap = r.snapshot()

    assert len(snap.turns) == 2
    assert snap.turns[1].gate is None  # the open turn was never graded
    revised = snap.turns[0].gate
    assert revised.word == "partial"  # the outcome now in force, not the delivered one
    assert revised.decision_id == "gate-e2a70c5b6d1f4a8390b4c7d2e5f60189"
    assert revised.supersedes == "gate-9b1f0f4ef4bcd390c47fa6c892ca4f89"
    assert 29 in snap.turns[0].control_seq
    assert [t.turn_id for t in delta.turns] == [1]  # the delta restates the owner


def test_the_supersedes_chain_is_walkable_from_the_last_word_to_the_first():
    """Three gradings, one phase: a reader walks the chain instead of guessing.

    `GateInfo.supersedes` is only worth carrying if every link it names is
    findable. The band holds every word delivered in the phase, in the order
    the gate layer delivered them, so walking back from the turn's current
    gate reaches the original grading and stops there.
    """
    r = TrajectoryReducer()
    for line in _lines(REAL_SILENT_PHASE_CALL_JSONL)[:4]:
        r.feed(line)
    r.feed(SYNTHETIC_GATE_REVISION_JSONL)
    r.feed(SYNTHETIC_GATE_AFTER_REVISION_JSONL)
    snap = r.snapshot()

    band = next(p for p in snap.phases if p.name == "provision")
    assert [g.word for g in band.gates] == ["success", "partial", "success"]

    chain = _walk(band.gates, snap.turns[0].gate)
    assert [g.decision_id for g in chain] == [
        "gate-77c1d4e9a05b46f2ac83be91d6407f52",
        "gate-e2a70c5b6d1f4a8390b4c7d2e5f60189",
        "gate-9b1f0f4ef4bcd390c47fa6c892ca4f89",
    ]


def test_a_revision_of_a_word_no_gate_delivered_is_an_orphan_not_a_guess():
    """`sag.agent.replay` aborts on this shape; a derivation states it and folds on.

    Replay is an integrity check and may refuse a stream that revises a word it
    never saw delivered. This layer is read-only observation: it has to keep
    folding, so the revision gets no owner, no turn is invented for it, and the
    band records that a word was delivered whose chain leads nowhere.
    """
    r = TrajectoryReducer()
    for line in _lines(REAL_SILENT_PHASE_CALL_JSONL)[:4]:
        r.feed(line)
    held = r.snapshot().turns[0].gate
    delta = r.feed(SYNTHETIC_ORPHAN_REVISION_JSONL)
    snap = r.snapshot()

    assert snap.turns[0].gate == held  # the graded turn keeps the word it was given
    assert delta.turns == []
    orphan = [w for w in snap.warnings if w.code == "orphan_gate_revision"]
    assert len(orphan) == 1 and orphan[0].control_seq == 31
    assert "gate-000000000000000000000000000000ff" in orphan[0].detail
    band = next(p for p in snap.phases if p.name == "provision")
    assert [g.decision_id for g in band.gates][-1] == "gate-e2a70c5b6d1f4a8390b4c7d2e5f60189"


def test_a_tool_result_for_an_envelope_no_turn_opened_is_stated():
    """[C] with no [B]: an answer to a call this ledger never recorded making."""
    r = TrajectoryReducer()
    delta = r.feed(REAL_TRIPLE[1])  # the kafka result, with its envelope withheld

    assert r.snapshot().turns == []
    assert [(w.code, w.control_seq) for w in delta.warnings] == [("orphan_tool_result", 4)]
    assert "envelope-000003" in delta.warnings[0].detail


def test_a_gate_event_carrying_no_word_is_stated_and_grades_nothing():
    """A gate whose outcome this version cannot read is a hole, not a crash."""
    r = TrajectoryReducer()
    for line in REAL_TRIPLE:
        r.feed(line)
    wordless = r.feed(SYNTHETIC_WORDLESS_GATE_JSONL)
    wordless_revision = r.feed(SYNTHETIC_WORDLESS_REVISION_JSONL)

    assert [(w.code, w.control_seq) for w in wordless.warnings] == [("gate_without_a_word", 32)]
    assert [(w.code, w.control_seq) for w in wordless_revision.warnings] == [
        ("gate_without_a_word", 33)
    ]
    assert r.snapshot().turns[0].gate is None
    # the band the call's own decision opened is there; no gate was added to it
    assert [(p.name, p.gates) for p in r.snapshot().phases] == [("provision", [])]


def test_a_gate_delivered_before_any_turn_opened_is_an_orphan():
    """A word with nothing to attach it to still bands its phase and says so."""
    r = TrajectoryReducer()
    delta = r.feed(_lines(REAL_SILENT_PHASE_CALL_JSONL)[3])  # the gate, alone

    assert r.snapshot().turns == []
    assert [(w.code, w.control_seq) for w in delta.warnings] == [("orphan_gate_decision", 27)]
    assert "'success'" in delta.warnings[0].detail
    band = r.snapshot().phases[0]
    assert band.name == "provision" and len(band.gates) == 1


def test_a_repair_context_opened_before_any_turn_is_an_orphan():
    """A repair context annotates the turn that provoked it; with none, it says so."""
    r = TrajectoryReducer()
    delta = r.feed(SYNTHETIC_REPAIR_CONTEXT_JSONL)

    assert r.snapshot().annotations == []
    assert [(w.code, w.control_seq) for w in delta.warnings] == [("orphan_repair_context", 122)]


def test_the_synthetic_turn_records_are_the_shape_the_engine_would_seal():
    """Built bytes prove nothing about the engine unless the engine could seal them."""
    for line in (SYNTHETIC_TURN_RECORD_JSONL, SYNTHETIC_TRUNCATED_TURN_RECORD_JSONL):
        TurnRecordPayload.model_validate(json.loads(line)["payload"])


def test_the_row_names_every_component_and_still_carries_one_handle():
    """[A] is a list; the row keeps the list AND the one handle it had before.

    `window_ref` answers "which window is this" in a table cell, and it stays
    the primary handle. But the quad view resolves the whole array, and a row
    that named only its last component made every earlier message of the window
    unreachable without re-reading the ledger — which is the archaeology the
    record exists to end.
    """
    r = TrajectoryReducer()
    for line in REAL_TRIPLE:
        r.feed(line)
    r.feed(SYNTHETIC_TRUNCATED_TURN_RECORD_JSONL)

    turn = r.snapshot().turns[0]
    assert turn.window_components == [
        "window_truncated:953",
        "output_aaaaaaaaaaaa",
        "output_bbbbbbbbbbbb",
        "output_cccccccccccc",
    ]
    # The newest message the model was shown — the one thing this turn did not
    # share with the turn before it.
    assert turn.window_ref == "output_cccccccccccc"


def test_a_turn_that_names_no_component_claims_no_window_either():
    """A controller answering from policy was shown nothing, and says so.

    An empty list is not a window with nothing in it; it is a record stating no
    components at all. `window_ref` has always read it that way, and the list
    reads it the same, so a row never carries half a claim.
    """
    r = TrajectoryReducer()
    r.feed(
        SYNTHETIC_TURN_RECORD_JSONL.replace(
            '"output_aaaaaaaaaaaa","output_bbbbbbbbbbbb","output_cccccccccccc"', ""
        )
    )

    empty = r.snapshot().turns[-1]
    assert empty.window_components is None and empty.window_ref is None


def test_a_third_observation_ref_does_not_leave_without_saying_so():
    """Three refs, two slots — and the one that cannot be carried is named.

    A row holds what the model READ and what the tool WROTE. When a decision
    has already contributed a second ref and the record then delivers a third,
    one of them cannot be carried. It used to leave in silence, which is the
    one thing a derivation may not do with a ref somebody wrote down.
    """
    envelope, result, decision = REAL_TRIPLE
    second_ref = decision.replace(
        '"evidence_ref":"output_6163859b019d"', '"evidence_ref":"output_9999abcd0000"'
    )
    assert second_ref != decision

    r = TrajectoryReducer()
    for line in (envelope, result, second_ref):
        r.feed(line)
    delta = r.feed(SYNTHETIC_TURN_RECORD_JSONL)

    turn = r.snapshot().turns[0]
    assert turn.observation.ref == "output_dddddddddddd"  # what the model read
    assert turn.observation.evidence_ref == "output_6163859b019d"  # what the tool wrote
    dropped = [w for w in delta.warnings if w.code == "observation_ref_dropped"]
    assert len(dropped) == 1 and "output_9999abcd0000" in dropped[0].detail
    assert dropped[0].turn_id == 1


def test_a_call_still_in_flight_is_not_a_ledger_that_does_not_balance():
    """The count fence is over the calls the ledger has FINISHED writing.

    Between an envelope and its answer every side of §2.2 rule 5 disagrees, and
    saying so on every event would state and withdraw a violation per call while
    the turn's own holes already say the same thing better.
    """
    envelope, result, decision = REAL_TRIPLE
    r = TrajectoryReducer()
    for line in (envelope, result, decision):
        r.feed(line)
        assert [w for w in r.snapshot().warnings if w.code == "conservation_violation"] == []


def test_the_reducer_has_no_detail_tier():
    """Tiers are a reader's concern; the fold is the same either way.

    `detail` selects how much a CONSUMER is handed (spec §3) — the builder
    resolves bytes, the CLI names the tier. The reducer never resolved a ref in
    its life, so carrying the word only invited a caller to believe it did.
    """
    with pytest.raises(TypeError):
        TrajectoryReducer(detail="full")
