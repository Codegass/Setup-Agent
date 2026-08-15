"""The reducer folds real control events into turns, and never raises.

Every fixture below is bytes lifted verbatim out of an archived session — the
kafka d2r3 run at
`logs/session_20260814_072758_651456_4d81644227c3_24117/control_events.jsonl`
and, for the controller turn, the httpcomponents-client run at
`logs/session_20260813_202604_931286_731b3b90c419_2417/control_events.jsonl`.
The field names the reducer joins on are the field names those runs actually
wrote; nothing here is invented.
"""

import pytest

from sag.trajectory.reducer import TrajectoryReducer

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


def test_the_order_of_the_triple_does_not_change_the_turn():
    """The engine writes envelope-first; a decision-first stream folds the same."""
    envelope, result, decision = REAL_TRIPLE
    forward = TrajectoryReducer()
    for line in (envelope, result, decision):
        forward.feed(line)
    reordered = TrajectoryReducer()
    for line in (decision, envelope, result):
        reordered.feed(line)
    assert len(reordered.snapshot().turns) == 1
    assert reordered.snapshot().turns[0].call == forward.snapshot().turns[0].call
    assert reordered.snapshot().turns[0].observation == forward.snapshot().turns[0].observation
    assert reordered.snapshot().warnings == []


def test_a_decision_with_no_result_seals_a_warning_when_the_next_turn_opens():
    first, second = _lines(REAL_TWO_DECISIONS_JSONL)
    r = TrajectoryReducer()
    opened = r.feed(first)
    assert opened.warnings == []  # nothing is missing until the turn is over
    sealed = r.feed(second)
    codes = {w.code for w in sealed.warnings}
    assert codes == {"missing_envelope", "missing_tool_result"}
    assert all(w.control_seq == 6 for w in sealed.warnings)
    assert len(r.snapshot().turns) == 2


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
    assert delta.session_patch == {"run_id": "r-1"}
    assert r.snapshot().session.run_id == "r-1"


def test_accumulated_deltas_reproduce_the_snapshot():
    """Upserting delta turns by turn_id is the same fold as snapshotting."""
    r = TrajectoryReducer()
    accumulated: dict[int, object] = {}
    for block in (REAL_TRIPLE_JSONL, REAL_FAILED_CALL_JSONL, REAL_SILENT_PHASE_CALL_JSONL):
        for line in _lines(block):
            for turn in r.feed(line).turns:
                accumulated[turn.turn_id] = turn
    snap = r.snapshot()
    assert [accumulated[t.turn_id] for t in snap.turns] == snap.turns


def test_the_detail_tier_is_summary_or_full_and_nothing_else():
    assert TrajectoryReducer(detail="full").detail == "full"
    with pytest.raises(ValueError):
        TrajectoryReducer(detail="verbose")
