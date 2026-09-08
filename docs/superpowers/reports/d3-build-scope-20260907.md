# Frozen d3 build scopes, 2026-09-07

All 23 original repository/revision pins, v1 file digests and matched cells were preserved. The final offline assembly completed without seat errors after the courier verified source bytes against each exact commit. Original checksum entries were verified before reconstruction; generated v2/addendum checksums were refreshed and new source-origin files were appended.

22/23 records now contain modules in at least one cell. At the frozen matched cell, 8/23 have a scope: 7 log, 1 test_bearing. The original v1 records had modules in 0/23 records. Missing scope remains unavailable; a zero in the table is not a measured zero-sized build.

| Seat | Frozen matched cell | Basis | Modules | CI command | Log archives |
| --- | --- | --- | ---: | --- | ---: |
| camel | build (25) | unavailable | unavailable | unavailable | 9 |
| camel-examples | build (21) | log | 70 | unavailable | 1 |
| camel-quarkus | unavailable | unavailable | unavailable | unavailable | 12 |
| cayenne | JDK 21, DB derby | log | 1 | mvn verify -q -DcayenneTestConnection=${{ matrix.db-profile }} -DcayenneLogLevel=ERROR | 1 |
| commons-csv | unavailable | unavailable | unavailable | unavailable | 1 |
| commons-dbcp | unavailable | unavailable | unavailable | unavailable | 1 |
| curator | Unit tests (17) | unavailable | unavailable | unavailable | 1 |
| freemarker | unavailable | unavailable | unavailable | unavailable | 1 |
| geode | acceptanceTest (ubuntu-latest, liberica, 17) | log | 48 | GRADLE_JVM_PATH=${JAVA_HOME_17_X64} JAVA_BUILD_PATH=${JAVA_HOME_17_X64} JAVA_BUILD_VERSION=17 JAVA_TEST_VERSION=17 cp gradlew gradlewStrict sed -e 's/JAVA_HOME/GRADLE_JVM/g' -i.back gradlewStrict GRADLE_JVM=${GRADLE_JVM_PATH} JAVA_TEST_PATH=${JAVA_TEST_PATH} ./gradlewStrict \   --no-parallel \   -PcompileJVM=${JAVA_BUILD_PATH} \   -PcompileJVMVer=${JAVA_BUILD_VERSION} \   -PtestJVM=${JAVA_TEST_PATH} \   -PtestJVMVer=${JAVA_TEST_VERSION} \   -PtestJava17Home=${JAVA_HOME_17_X64} \   acceptanceTest --console=plain --no-daemon | 1 |
| httpcomponents-client | build (ubuntu-latest, 17) | log | 9 | unavailable | 1 |
| ignite | Check java code on JDK 17 | unavailable | unavailable | unavailable | 1 |
| jackrabbit | unavailable | unavailable | unavailable | unavailable | 1 |
| kafka | junit-xml-17-noflaky-nonew (jdk 17) | test_bearing | 34 | unavailable | 112 |
| lucene | Smoke test release on jdk 25, ubuntu-latest | unavailable | unavailable | unavailable | 13 |
| ofbiz-framework | build (17) | log | 2 | unavailable | 3 |
| polaris | unavailable | unavailable | unavailable | unavailable | 3 |
| rocketmq | maven-compile (ubuntu-latest, JDK-8) | log | 19 | unavailable | 5 |
| seatunnel | Run / all-connectors-it-1 (11, ubuntu-latest) | unavailable | unavailable | unavailable | 10 |
| spark-kubernetes-operator | Run / Build Test CI (ubuntu-26.04, 21) | unavailable | unavailable | unavailable | 183 |
| storm | unavailable | unavailable | unavailable | unavailable | 8 |
| struts | unavailable | unavailable | unavailable | unavailable | 2 |
| tomcat-jakartaee-migration | unavailable | unavailable | unavailable | unavailable | 2 |
| zookeeper | compatibility (17, 3.7.2) | log | 14 | mvn -B -V -e -ntp "-Dstyle.color=always" package -DskipTests | 3 |

## Kafka and Tomcat evidence limits

Kafka remains `test_bearing`, 34 modules, at frozen commit `f5e01c7b0a07878e79d310a021a7881c6ee52717`. Of 112 available run-log archives, 38 job logs name build modules. Two main JDK17 job logs each name 69 modules, but the existing pool/check containment rule covers three distinct pools (main, new, flaky); there is no unique provenance link to the frozen matched pool. The harvester therefore refuses to promote one arbitrarily. The fetched settings file also uses `includeBuild` and `project(":storage:api").name = "storage-api"`; the bounded reader cannot prove the complete graph or resolve that project-name alias. This is not the literal `projectDir` assignment assumed in the design sketch. `storage/api` remains disclosed as unmatched; no Kafka-specific alias is invented.

The separate archived Kafka acceptance remains 18/34 with one unmatched observed `storage/api`, using CI run 27721225836 at commit `26b251a451ce941d3d7a55e6487bcb7f16b5ad48` and the archived SAG evidence tree. It is not a live Kafka build or a reattribution of those old artifacts to the d3 pin.

Tomcat is a single-module root build. Its JDK11 Windows cell now carries one declared module. Its frozen matched cell stays unavailable: the archived workflow failure-swallowing rule marks all six conclusion-only cells ineligible. Reassembly does not choose a new cell to obtain a more favorable comparison.

Commands and source scopes remain conservative when matrix expressions, multi-command scripts, profile properties, composite Gradle builds, absent child POMs, or the 400-child-POM courier bound prevent proof. The courier reached that bound for camel and camel-quarkus. This report does not claim every CI universe became measurable.

## Frozen v1 and reassembled v2 digests

| Seat | Original v1 SHA-256 | v2 SHA-256 |
| --- | --- | --- |
| camel | `9819d4c11a3a2dfec8d208d55dafc68e1885dab795996607a19d761d13e86532` | `8e239c729d933f53978f316b3c12b449cfe1ec88f3f76755116d7bdcd7de962d` |
| camel-examples | `d27b3d206b96923438f0320aea778b4523d351fb182153cba13ab860a00df574` | `e5ccfc8db4996725859952986263ba141dcc68b7ec5e083e475ba73315150f95` |
| camel-quarkus | `c08093622f600102cd81ab67481620fdb5f91b01ac114b617c670f552d154b3c` | `a5c9f5ac8d3db95f5474cf1736948110f1166e8b25b7b0c20abf73fb2505f456` |
| cayenne | `1c18f7d780cc8d68e9f3279819cd6a14ce4520e138cc2b30c5e4113b3e8fad79` | `71baad4b13275cee33d169ed7402501097c2c1c9306d2419c674ebdfffa0380a` |
| commons-csv | `c4c76036dac2ba67e98f1811068cf12eb31d97633b95fd1474943e69b3b963f1` | `ee01feb33af44a7055bd64af0883e204617eb19be1c21c0fc7d009cb070de80f` |
| commons-dbcp | `e65e0e9c87b865d81bc42dd0c2dec3fb7043db567c95d48bf0ede45148249b01` | `389dc0cff39899bab4f76b0525df00699e32c61e681b3311c5e5d0634035d396` |
| curator | `fe8090a93cf4859eb35ad24c5b506fe68adb2d1a81d6df84f9698cb8a057134f` | `1aea3741909a94d2d39d43cb83716483a2408345b7b1df955b726b7af5deca17` |
| freemarker | `af4a17c5ad93952a7480f88df84d683c6b23953cb69d462aa9ada551e9de2692` | `f32ab7f40d64126299e1946299f55999d8ced48c10efeff391b87dcb80709efc` |
| geode | `4f201452125f62945e257d9a6bc3d6bd6b0e05c15ddcc1b572f679a5e5aeed37` | `c92b8fd8057b910293847142790ccb22a90a9f4a8cb005e0f61ae9d2c766661d` |
| httpcomponents-client | `40a9cdfb850694c8c746a5bcf1016b5b946a5d9e6366b971a52bac3623be39f7` | `b830280cc46d461292f2bd76170e07de8618b72950f7fa5fb75da917da2049bf` |
| ignite | `9f1ef4f96850b1bb016bdd0658dae8ba0d12b90375e58d98d99bbe81ea641ea1` | `8ff2e4d4c6a0e13ee2d6681a16b8675c9fd2a35bc5b11a42504c153f31f983d9` |
| jackrabbit | `c0967f05f5e842f738c2851ab52a519a664214600ef7165cac4fc523a80b8891` | `f50eabf28ff8bfbbaf5e4157a4335a9e86845f24b98bce157c58f2256f19d4a3` |
| kafka | `ab11fd0331755a402755ac3be5985da0b9b49310edb095c8c102543c84fb2cb1` | `70ca35aebef0d0eb5408508f9ad4ef62ae41c33f283b7093196c388eb6830bbe` |
| lucene | `d455f77d34c8b92cc454044712e4215c0e202a8c35fc72962fb5ac336ea63d86` | `737310ee959bb651d051876508b03c02e01c0f76c76ba6a4d04be92724755912` |
| ofbiz-framework | `5e472a0f015fa51c8d6cca582030c9d58ca0a4363fd775891d2432d61d3dcffc` | `e77bd1c34d208fa39cb6d7a00f0196b8bf6c2bee6207afc2ef81910c17aa264d` |
| polaris | `c885687430c1cfe703a60b6dcc79fc2c910be73633001ea3cacf6b8a678b74c3` | `1abf999da04a0d52e03d1a1c9fff7f780113e72cfe4e8ed198669461d2d099b8` |
| rocketmq | `47514de56056ae5bf532bac4b89eb281e89bb2e776d5fc7e59538c93193de43d` | `1c210751c8a784396b44d99b7cef2cec08c0884e35520da748964880cda8e6a4` |
| seatunnel | `674a46dd2a24c2bc8bf5a9652898fb7a73dc18b70ab65410aa12e63e244345b5` | `e2307649dfb2db185f5d4ee03ae39206f001e9bee3e8c4eb7873057d934f2c87` |
| spark-kubernetes-operator | `bacd4edcbf02faadb416e67a0930eefa1f8a413b6c3971f69abbcf20c5dff770` | `adcfb2cd17feea5a1f1c95c7e9f8b4f718151cbca4882d0c18e27996cb2d343e` |
| storm | `03ea01e834a2dd95323da2959a22bc6f3a2f590d888b9d4ad7a79e74b9b19077` | `688ff0b04154fe6a49b78e76c926124e2ccabe2f4fc94309770610fb97961a5f` |
| struts | `af090d8401bc20bedd0ee8b3a6390cf96db59100d506655fb6e696e5407ef47d` | `032bfd9832a9a4b016273e2ef14207372a66c76d7b7893562914800e34a4fa3c` |
| tomcat-jakartaee-migration | `d71326ff9d9bed86e8eb138b53d46c010102873554a275bf902a1a449b19f41b` | `453633a1997f4092afeada520ef2744994e2487abf4a309426ee14b831f5828c` |
| zookeeper | `9f1f188c29c78207bd9c45e231f87e62a3a4113fa8d581ab49b6a79cef216725` | `6e99244f064f42bbcd6d34fff44876c0b32d3ff74d01eefa9caf2f35f125321a` |

## Reproduction

Run `uv run python scripts/d3_reassemble_v2.py` against the local frozen archive. Add `--refetch` only for the authorized `gh api` courier. The script validates existing frozen inputs first and reports per-seat courier/assembly errors without abandoning checksum publication for completed seats. Raw archives remain under the gitignored `logs/d3-freeze-20260830/`; the report and driver are committed.
