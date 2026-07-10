"""ReadModelBuilder.system() — nav-bar resource stats, best-effort per source."""

from sag.web.read_model import ReadModelBuilder


class _FakeClient:
    def df(self):
        return {
            "LayersSize": 1000,
            "Volumes": [{"UsageData": {"Size": 500}}, {"UsageData": {"Size": -1}}],
            "Containers": [{"SizeRw": 200}],
            "BuildCache": [{"Size": 40}, {"Size": 10}],
        }


class _FakeRegistry:
    client = _FakeClient()


def test_docker_df_summed():
    s = ReadModelBuilder(workspace_registry=_FakeRegistry()).system()
    assert s.docker_disk_used == 1700  # 1000 layers + 500 vol (-1 clamped) + 200 container
    assert s.docker_reclaimable == 50


class _BrokenRegistry:
    @property
    def client(self):
        raise RuntimeError("docker down")


def test_docker_unavailable_is_none_not_error():
    s = ReadModelBuilder(workspace_registry=_BrokenRegistry()).system()
    assert s.docker_disk_used is None
    assert s.docker_reclaimable is None
    # host stats still attempted; on Linux mem_total is populated, else None — no raise
    assert s.mem_total is None or s.mem_total > 0
