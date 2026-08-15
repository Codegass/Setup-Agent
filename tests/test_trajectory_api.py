"""The trajectory is one GET away, whole or since a turn.

The web app's read path for a session's trajectory. Two things are being fenced
here and they pull in opposite directions, which is why they are fenced
together:

- **It is the same derivation.** The endpoint serves what `build_trajectory`
  derives from the session directory — byte-for-byte the document the CLI
  prints and the golden fences pin. A second derivation living in the web layer
  is exactly what spec §4 forbids.
- **It never `docker exec`s.** A running session is read through
  `session_mirror.ensure_mirror`, the host mirror that `get_archive`s a
  container's result files without entering it. The dashboard polls this
  endpoint; a read path that exec'd would revive stopped containers and hammer
  the daemon, which is the whole reason the mirror exists.

The kafka d2r3 fixture is mounted the way the app meets a real session: a host
`logs/session_*/` holding the ledger the engine wrote, and a host mirror holding
the container's `.setup_agent/` — the trunk context that says which project this
workspace ran, which is how a session id resolves to a directory at all.
"""

import json
import shutil
from pathlib import Path

from fastapi.testclient import TestClient

from sag.trajectory.builder import build_trajectory
from sag.web import session_mirror
from sag.web.app import create_app
from sag.web.models import DockerSummary, WorkspaceSummary
from sag.web.read_model import ReadModelBuilder
from sag.web.session_mirror import MirrorReader
from sag.web.session_registry import ContainerSessionRegistry

FIXTURES = Path(__file__).parent / "fixtures" / "trajectory"
KAFKA = FIXTURES / "kafka-d2r3"
IGNITE = FIXTURES / "ignite-d2r3"

#: The trunk's context id carries the timestamp the session id is built from,
#: so the id below is derived, not invented — `_setup_session_id`'s own rule.
TRUNK = "trunk_20260814_072758"


def _session_id(project: str) -> str:
    return f"SETUP-{project}-20260814-072758"


def _mount(tmp_path: Path, *, fixture: Path = KAFKA, project: str = "kafka", ledger: bool = True):
    """Mount an archived session the way the app meets a live one.

    The host session directory is where the engine wrote its ledger; the mirror
    is the container's `.setup_agent/`, fetched without an exec. Both exist for
    a live run, and the trunk in the mirror is what names the project.
    """
    logs = tmp_path / "logs"
    session_dir = logs / "session_20260814_072758_651456_4d81644227c3_24117"
    session_dir.mkdir(parents=True)
    (session_dir / f"command_project_{project}.log").write_text("setup\n", encoding="utf-8")
    if ledger:
        shutil.copy(fixture / "control_events.jsonl", session_dir / "control_events.jsonl")
        shutil.copy(fixture / "token_usage.csv", session_dir / "token_usage.csv")

    contexts = logs / "web_mirror" / f"sag-{project}" / ".setup_agent" / "contexts"
    contexts.mkdir(parents=True)
    (contexts / f"{TRUNK}.json").write_text(
        json.dumps(
            {
                "context_id": TRUNK,
                "project_name": project,
                "created_at": "2026-08-14T07:27:58",
                "goal": "Set up the project",
            }
        ),
        encoding="utf-8",
    )
    return logs, session_dir, contexts.parent.parent


class _Workspaces:
    def __init__(self, project: str) -> None:
        self.project = project

    def list_workspaces(self):
        return [
            WorkspaceSummary(
                id=f"sag-{self.project}",
                project=self.project,
                container=f"sag-{self.project}",
                docker=DockerSummary(status="running"),
            )
        ]


def _client(logs: Path, mirror: Path, project: str = "kafka") -> TestClient:
    registry = ContainerSessionRegistry(
        orchestrator_factory=lambda workspace_id: MirrorReader(mirror),
        workspace_registry_factory=lambda: _Workspaces(project),
        logs_root=logs,
    )
    return TestClient(create_app(ReadModelBuilder(session_registry=registry)))


def test_the_trajectory_of_a_mirrored_session_is_one_get_away(tmp_path):
    """kafka's twenty-four calls, over HTTP, from a session nobody exec'd into."""
    logs, _, mirror = _mount(tmp_path)

    response = _client(logs, mirror).get(f"/api/sessions/{_session_id('kafka')}/trajectory")

    assert response.status_code == 200
    body = response.json()
    assert body["schema_version"] == 1
    assert sum(1 for turn in body["turns"] if turn["call"] is not None) == 24
    assert body["outputs"] is None  # the summary tier resolves no bytes


def test_the_endpoint_serves_the_document_the_replay_derives(tmp_path):
    """One derivation (spec §4): the wire document IS `build_trajectory`'s."""
    logs, session_dir, mirror = _mount(tmp_path)

    response = _client(logs, mirror).get(f"/api/sessions/{_session_id('kafka')}/trajectory")

    assert response.json() == build_trajectory(session_dir).model_dump(mode="json")


def test_a_since_cut_returns_only_the_turns_after_it(tmp_path):
    """The cut is over TURNS, and over nothing else.

    Everything a poller needs to converge — the session, the bands, the
    annotations, the warnings — is restated whole, because a warning that
    retracts and an annotation that lands on an older turn have no other channel
    in a stateless GET.
    """
    logs, _, mirror = _mount(tmp_path)
    client = _client(logs, mirror)
    session = _session_id("kafka")

    whole = client.get(f"/api/sessions/{session}/trajectory").json()
    cut = client.get(f"/api/sessions/{session}/trajectory?since=20").json()

    assert cut["turns"] == [turn for turn in whole["turns"] if turn["turn_id"] > 20]
    assert cut["turns"] and len(cut["turns"]) < len(whole["turns"])
    assert cut["warnings"] == whole["warnings"] and whole["warnings"]
    assert cut["annotations"] == whole["annotations"]
    assert cut["session"] == whole["session"]
    assert cut["phases"] == whole["phases"]
    assert cut["schema_version"] == whole["schema_version"]


def _ledger(session_dir: Path, fixture: Path, lines: int | None = None) -> int:
    """Write the first `lines` lines of a fixture's ledger, as a run writing it.

    A live ledger is a file that grows. Truncating an archived one is how a poll
    that catches a run mid-turn is reproduced without inventing a single byte:
    every line is the engine's own.
    """
    raw = (fixture / "control_events.jsonl").read_text(encoding="utf-8").splitlines(True)
    kept = raw if lines is None else raw[:lines]
    (session_dir / "control_events.jsonl").write_text("".join(kept), encoding="utf-8")
    return len(kept)


def test_a_turn_still_open_at_the_cut_is_restated_when_the_ledger_fills_it(tmp_path):
    """The poller's cut is over the LEDGER, not over the turn ids it has seen.

    kafka's first turn is opened by its `action_envelope` (seq 3) and finished by
    the `tool_result` (seq 4) and `loop_decision` (seq 6) that answer it, thirteen
    seconds later. A poll landing in between holds that turn half-stated — phase
    `unknown`, no iteration, no observation, no bill, `control_seq [3]` — and its
    holes are stated as warnings.

    A turn-id cut can never repair that: `turn_id <= since` drops the turn
    forever, while the whole-state rule withdraws the warnings that named its
    holes, leaving a row that is wrong and no longer says so. The sequence cut
    restates every turn the ledger has touched since the consumer last read.
    """
    logs, session_dir, mirror = _mount(tmp_path)
    _ledger(session_dir, KAFKA, lines=3)
    client = _client(logs, mirror)
    session = _session_id("kafka")

    open_turn = client.get(f"/api/sessions/{session}/trajectory").json()["turns"][0]
    assert open_turn["control_seq"] == [3] and open_turn["iteration"] is None

    _ledger(session_dir, KAFKA, lines=8)
    by_turn = client.get(f"/api/sessions/{session}/trajectory?since=1").json()
    caught_up = client.get(f"/api/sessions/{session}/trajectory?since_seq=3").json()

    # The cut the turn id gives is the turn that was open — it drops itself.
    assert 1 not in [turn["turn_id"] for turn in by_turn["turns"]]
    restated = next(turn for turn in caught_up["turns"] if turn["turn_id"] == 1)
    assert restated["phase"] == "provision"
    assert restated["iteration"] == 1
    assert restated["observation"]["ref"] == "output_6163859b019d"
    assert restated["tokens"] == {"input": 4134, "output": 71}
    assert restated["t1"] == "2026-08-14T11:28:49.545505Z"
    assert restated["control_seq"] == [3, 4, 6]


def test_the_sequence_cut_drops_only_the_turns_the_ledger_has_not_touched(tmp_path):
    """Everything else is the `since=` contract, unchanged."""
    logs, _, mirror = _mount(tmp_path)
    client = _client(logs, mirror)
    session = _session_id("kafka")

    whole = client.get(f"/api/sessions/{session}/trajectory").json()
    cut = client.get(f"/api/sessions/{session}/trajectory?since_seq=100").json()

    assert cut["turns"] == [turn for turn in whole["turns"] if max(turn["control_seq"]) > 100]
    assert cut["turns"] and len(cut["turns"]) < len(whole["turns"])
    assert cut["warnings"] == whole["warnings"] and whole["warnings"]
    assert cut["annotations"] == whole["annotations"]
    assert cut["session"] == whole["session"]
    assert cut["phases"] == whole["phases"]


def test_a_late_fold_onto_a_turn_reaches_the_poller_the_turn_cut_lost(tmp_path):
    """ignite's job close (seq 240) lands on the turn that dispatched it (235).

    The turn was opened, answered and decided by seq 239; the `job_live_at_close`
    at 240 folds onto it afterwards, and the slice corpus pins its
    `control_seq` as `[235, 238, 239, 240]` — the handle a reader descends with.
    A consumer that had already been sent that turn is never sent it again under
    a turn-id cut, so the fold is lost to every poller. Under the sequence cut it
    is exactly what comes back.
    """
    logs, session_dir, mirror = _mount(tmp_path, fixture=IGNITE, project="ignite")
    client = _client(logs, mirror, project="ignite")
    session = _session_id("ignite")

    whole = client.get(f"/api/sessions/{session}/trajectory").json()
    dispatch = next(turn for turn in whole["turns"] if 240 in turn["control_seq"])
    assert dispatch["control_seq"] == [235, 238, 239, 240]

    by_turn = client.get(f"/api/sessions/{session}/trajectory?since={dispatch['turn_id']}").json()
    by_seq = client.get(f"/api/sessions/{session}/trajectory?since_seq=239").json()

    assert by_turn["turns"] == []
    assert [turn["turn_id"] for turn in by_seq["turns"]] == [dispatch["turn_id"]]


def test_polling_a_growing_ledger_converges_on_the_run(tmp_path):
    """The fence the cut exists for: a poller ends up holding the whole run.

    kafka's ledger is replayed a slice at a time, exactly as a live run appends
    it, and each poll asks for the turns touched since the last line the consumer
    held — merging as the timeline merges: upsert turns by id, replace everything
    else. What the poller is left with must be the document a replay of the
    finished file derives, turn for turn, byte for byte. Under a turn-id cut this
    diverged on every turn that was ever caught in flight.
    """
    logs, session_dir, mirror = _mount(tmp_path)
    total = _ledger(session_dir, KAFKA)
    client = _client(logs, mirror)
    session = _session_id("kafka")

    held: dict | None = None
    for stop in (*range(1, total, 11), total):
        _ledger(session_dir, KAFKA, lines=stop)
        watermark = (
            max((seq for turn in held["turns"] for seq in turn["control_seq"]), default=None)
            if held
            else None
        )
        query = "" if watermark is None else f"?since_seq={watermark}"
        polled = client.get(f"/api/sessions/{session}/trajectory{query}").json()
        if held is None:
            held = polled
            continue
        turns = {turn["turn_id"]: turn for turn in held["turns"]}
        turns.update({turn["turn_id"]: turn for turn in polled["turns"]})
        held = {**polled, "turns": [turns[key] for key in sorted(turns)]}

    assert held == client.get(f"/api/sessions/{session}/trajectory").json()


def test_two_cuts_at_once_are_refused_by_name(tmp_path):
    """One read, one watermark. A response cut two ways states neither."""
    logs, _, mirror = _mount(tmp_path)

    response = _client(logs, mirror).get(
        f"/api/sessions/{_session_id('kafka')}/trajectory?since=3&since_seq=3"
    )

    assert response.status_code == 422
    assert "since" in response.json()["detail"] and "since_seq" in response.json()["detail"]


def test_a_cut_past_the_last_turn_still_states_the_run(tmp_path):
    """Nothing new is not nothing: the holes are stated on every poll."""
    logs, _, mirror = _mount(tmp_path)
    client = _client(logs, mirror)
    session = _session_id("kafka")

    whole = client.get(f"/api/sessions/{session}/trajectory").json()
    cut = client.get(f"/api/sessions/{session}/trajectory?since=9999").json()

    assert cut["turns"] == []
    assert cut["warnings"] == whole["warnings"]


def test_a_cut_carries_an_annotation_that_landed_on_an_older_turn(tmp_path):
    """ignite's forced dispatch is annotated on ITS turn, below the cut.

    An annotation is a fact about a turn that already happened, so a consumer
    appending them per poll would double every one — and a consumer that never
    saw the older turn would miss the annotation entirely. Whole state, replaced.
    """
    logs, _, mirror = _mount(tmp_path, fixture=IGNITE, project="ignite")
    client = _client(logs, mirror, project="ignite")
    session = _session_id("ignite")

    whole = client.get(f"/api/sessions/{session}/trajectory").json()
    forced = next(a for a in whole["annotations"] if a["kind"] == "forced")
    cut = client.get(f"/api/sessions/{session}/trajectory?since={forced['turn_id'] + 1}").json()

    assert forced["turn_id"] not in [turn["turn_id"] for turn in cut["turns"]]
    assert forced in cut["annotations"]


def _second_run(logs: Path, fixture: Path, project: str) -> Path:
    """A LATER host session directory that ran the same project.

    The real state of this machine: `logs/` holds 183 session directories and
    kafka, ignite, camel-quarkus and polaris each ran twice, both runs leaving a
    ledger. Newest-by-mtime is therefore a choice between two runs, made every
    time a session id is resolved.
    """
    later = logs / "session_20260814_074153_238028_5398df380672_24385"
    later.mkdir(parents=True)
    (later / f"command_project_{project}.log").write_text("setup\n", encoding="utf-8")
    shutil.copy(fixture / "control_events.jsonl", later / "control_events.jsonl")
    return later


def _pin(mirror: Path, fixture: Path) -> None:
    """The run-pin the container publishes at startup, as the mirror holds it."""
    shutil.copy(fixture / "run-pin.json", mirror / ".setup_agent" / "run-pin.json")


def test_the_run_the_id_names_is_the_run_that_answers(tmp_path):
    """Two runs of one project, and the id resolves to the run it names.

    `_matching_log_session_dir` picks the NEWEST host directory carrying this
    project's command log. When the mirror's trunk lags the host logs — a stopped
    container is mirrored once and never refetched — the id names the first run
    while the newest directory holds the second, and the endpoint served the
    second run's whole document under the first run's id: its run_id, its turns,
    its warnings and, at `detail=full`, its BYTES.

    The container publishes a run-pin at startup and the mirror carries it, so
    the run this id names is stated rather than guessed. The directory that
    answers is the one whose ledger claims that run.
    """
    logs, first, mirror = _mount(tmp_path)
    _second_run(logs, IGNITE, "kafka")
    _pin(mirror, KAFKA)

    body = _client(logs, mirror).get(f"/api/sessions/{_session_id('kafka')}/trajectory").json()

    assert body["session"]["run_id"] == "20260814_072758_651456_4d81644227c3_24117-7-b9b1b06dfff3"
    assert sum(1 for turn in body["turns"] if turn["call"] is not None) == 24
    assert body == build_trajectory(first).model_dump(mode="json")


def test_a_run_nothing_names_is_refused_rather_than_guessed_at(tmp_path):
    """Nothing states which run this id is, and two directories could answer.

    Serving the newest of them is a guess, and a guess presented as a run's
    evidence is the failure this program exists to end. The reader is told what
    could not be decided, and which directories it was decided between.
    """
    logs, _, mirror = _mount(tmp_path)
    later = _second_run(logs, IGNITE, "kafka")

    response = _client(logs, mirror).get(f"/api/sessions/{_session_id('kafka')}/trajectory")

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert "session_20260814_072758_651456_4d81644227c3_24117" in detail
    assert later.name in detail


def test_the_containers_own_copy_answers_for_a_run_the_host_never_kept(tmp_path):
    """The pin names a run no host directory holds — the mirror is that run.

    A pin that matches nothing on the host is not a reason to serve a stranger's
    ledger. The container mirrored its own control events, and those are the run
    the id names.
    """
    logs, session_dir, mirror = _mount(tmp_path, ledger=False)
    _ledger(session_dir, IGNITE)  # the host's only kafka directory is another run
    _pin(mirror, KAFKA)
    shutil.copy(KAFKA / "control_events.jsonl", mirror / ".setup_agent" / "control_events.jsonl")

    body = _client(logs, mirror).get(f"/api/sessions/{_session_id('kafka')}/trajectory").json()

    assert body["session"]["run_id"] == "20260814_072758_651456_4d81644227c3_24117-7-b9b1b06dfff3"
    assert sum(1 for turn in body["turns"] if turn["call"] is not None) == 24


def test_the_full_tier_is_asked_for_by_name(tmp_path):
    """`detail=full` is what makes the endpoint resolve refs to bytes at all."""
    logs, _, mirror = _mount(tmp_path)
    client = _client(logs, mirror)
    session = _session_id("kafka")

    summary = client.get(f"/api/sessions/{session}/trajectory?detail=summary").json()
    full = client.get(f"/api/sessions/{session}/trajectory?detail=full").json()

    assert summary["outputs"] is None
    assert full["outputs"] is not None


def test_a_detail_tier_this_version_does_not_have_is_refused_by_name(tmp_path):
    logs, _, mirror = _mount(tmp_path)

    response = _client(logs, mirror).get(
        f"/api/sessions/{_session_id('kafka')}/trajectory?detail=verbose"
    )

    assert response.status_code == 422
    assert "verbose" in response.json()["detail"]


def test_an_unknown_session_is_a_404_not_an_empty_trajectory(tmp_path):
    """An empty document is a claim about a run. A missing session makes none."""
    logs, _, mirror = _mount(tmp_path)

    response = _client(logs, mirror).get("/api/sessions/SETUP-kafka-19700101-000000/trajectory")

    assert response.status_code == 404
    assert response.json()["detail"] == "Session not found: SETUP-kafka-19700101-000000"


def test_a_session_that_has_not_written_its_ledger_yet_is_a_warning_not_an_error(tmp_path):
    """Attaching before the first event is how watching a run START looks."""
    logs, _, mirror = _mount(tmp_path, ledger=False)

    response = _client(logs, mirror).get(f"/api/sessions/{_session_id('kafka')}/trajectory")

    assert response.status_code == 200
    body = response.json()
    assert body["turns"] == []
    assert [warning["code"] for warning in body["warnings"]] == ["missing_control_events"]


def test_when_the_host_kept_no_ledger_the_containers_mirrored_copy_answers(tmp_path):
    """The mirror is a session directory too — `.setup_agent/` is a root.

    The engine mirrors every control event into the container as it writes it,
    and `ensure_mirror` brings that copy to the host without an exec. A host
    that never held the ledger (the UI running from another checkout) therefore
    still has a trajectory to serve.
    """
    logs, _, mirror = _mount(tmp_path, ledger=False)
    shutil.copy(KAFKA / "control_events.jsonl", mirror / ".setup_agent" / "control_events.jsonl")

    response = _client(logs, mirror).get(f"/api/sessions/{_session_id('kafka')}/trajectory")

    assert response.status_code == 200
    assert sum(1 for turn in response.json()["turns"] if turn["call"] is not None) == 24


def test_a_live_session_is_read_through_the_mirror_and_never_exec_d(tmp_path, monkeypatch):
    """The production read path, with the docker client faked and nothing else.

    `ensure_mirror` is asked for the RUNNING container's files and the reader
    that answers is a `MirrorReader` — a reader over host files with no exec in
    it. This is the fence for plan Task 12's "the endpoint never `docker exec`s".
    """
    logs, _, mirror = _mount(tmp_path)
    fetched = []

    def fake_ensure_mirror(client, container_name, running, logs_root, *args, **kwargs):
        fetched.append((container_name, running, Path(logs_root)))
        return mirror

    monkeypatch.setattr(session_mirror, "ensure_mirror", fake_ensure_mirror)
    registry = ContainerSessionRegistry(
        workspace_registry_factory=lambda: _Workspaces("kafka"),
        logs_root=logs,
    )
    registry._client = object()  # a docker client that can archive, and cannot exec
    client = TestClient(create_app(ReadModelBuilder(session_registry=registry)))

    response = client.get(f"/api/sessions/{_session_id('kafka')}/trajectory")

    assert response.status_code == 200
    assert sum(1 for turn in response.json()["turns"] if turn["call"] is not None) == 24
    assert fetched == [("sag-kafka", True, logs)]
