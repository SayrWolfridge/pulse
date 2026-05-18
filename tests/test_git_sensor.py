"""Tests for the Git sensor (Phase 3 integration)."""

import asyncio
import os
import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pulse.src.core.config import PulseConfig, GitSensorConfig
from pulse.src.sensors.git_sensor import GitSensor, _RepoState
from pulse.src.drives.engine import DriveEngine, Drive
from pulse.src.state.persistence import StatePersistence


# ---- Fixtures ----


@pytest.fixture
def git_config(tmp_path):
    """Build a PulseConfig with Git sensor enabled, pointing at a temp dir."""
    config = PulseConfig()
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    config.sensors.git = GitSensorConfig(
        enabled=True,
        repos=[str(repo_dir)],
        stale_push_minutes=60,
        fetch_remote=False,
        request_timeout=5,
    )
    config.state = MagicMock()
    config.state.dir = str(tmp_path / "state")
    return config


@pytest.fixture
def sensor(git_config, tmp_path):
    return GitSensor(git_config)


# ---- Helper to mock _run_git results per command ----

def make_run_git_mock(responses: dict):
    """Return an AsyncMock for _run_git keyed by first git arg.

    ``responses`` maps the first arg (e.g. "status") to the stdout string
    or None (error). Special key "rev-parse" maps to ``["rev-parse", "--git-dir"]``.
    """
    async def _mock(args, cwd):
        key = args[0]
        if key == "rev-parse":
            return responses.get("rev-parse", ".git")
        if key == "status":
            return responses.get("status", "")
        if key == "rev-list":
            return responses.get("rev-list", "0\t0")
        if key == "log":
            return responses.get("log", str(int(time.time())))
        if key == "fetch":
            return responses.get("fetch", "")
        return None
    return _mock


# ---- Config parsing ----


class TestConfigParsing:
    def test_default_config(self):
        config = PulseConfig()
        assert config.sensors.git.enabled is False
        assert config.sensors.git.repos == []
        assert config.sensors.git.stale_push_minutes == 60
        assert config.sensors.git.fetch_remote is False
        assert config.sensors.git.request_timeout == 10

    def test_yaml_round_trip(self, tmp_path):
        """Config values load from YAML correctly."""
        yaml_content = """
sensors:
  git:
    enabled: true
    repos:
      - ~/my-repo
      - /tmp/other-repo
    stale_push_minutes: 30
    fetch_remote: true
    request_timeout: 15
"""
        yaml_file = tmp_path / "pulse.yaml"
        yaml_file.write_text(yaml_content)
        config = PulseConfig.load(str(yaml_file))
        assert config.sensors.git.enabled is True
        assert config.sensors.git.repos == ["~/my-repo", "/tmp/other-repo"]
        assert config.sensors.git.stale_push_minutes == 30
        assert config.sensors.git.fetch_remote is True
        assert config.sensors.git.request_timeout == 15


# ---- Lifecycle ----


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_initialize_valid_repo(self, sensor, git_config, tmp_path):
        """Sensor initializes when repo path exists and git is available."""
        with patch.object(sensor, "_git_available", new_callable=AsyncMock, return_value=True):
            await sensor.initialize()
        # Should have registered the repo path
        repo_path = str(Path(git_config.sensors.git.repos[0]).expanduser().resolve())
        assert repo_path in sensor._repo_states

    @pytest.mark.asyncio
    async def test_initialize_missing_repo(self, tmp_path):
        """Sensor skips nonexistent repo paths gracefully."""
        config = PulseConfig()
        config.sensors.git = GitSensorConfig(
            enabled=True,
            repos=[str(tmp_path / "does-not-exist")],
        )
        s = GitSensor(config)
        with patch.object(s, "_git_available", new_callable=AsyncMock, return_value=True):
            await s.initialize()
        assert len(s._repo_states) == 0

    @pytest.mark.asyncio
    async def test_initialize_no_git_binary(self, sensor):
        """Sensor warns when git is not installed."""
        with patch.object(sensor, "_git_available", new_callable=AsyncMock, return_value=False):
            await sensor.initialize()
        assert len(sensor._repo_states) == 0

    @pytest.mark.asyncio
    async def test_stop_is_noop(self, sensor):
        """Stop completes without error."""
        await sensor.stop()


# ---- Read: clean repo ----


class TestReadClean:
    @pytest.mark.asyncio
    async def test_clean_repo_no_flags(self, sensor, git_config, tmp_path):
        """A clean repo with no uncommitted changes returns all-clear."""
        repo_path = str(Path(git_config.sensors.git.repos[0]).expanduser().resolve())
        sensor._repo_states[repo_path] = _RepoState()

        now_ts = str(int(time.time()))
        mock_fn = make_run_git_mock({
            "rev-parse": ".git",
            "status": "",
            "rev-list": "0\t0",
            "log": now_ts,
        })
        with patch.object(sensor, "_run_git", side_effect=mock_fn):
            result = await sensor.read()

        assert result["uncommitted_changes"] is False
        assert result["untracked_files"] == 0
        assert result["commits_ahead"] == 0
        assert result["commits_behind"] == 0
        assert result["stale_push"] is False
        assert result["last_commit_minutes_ago"] is not None
        assert result["last_commit_minutes_ago"] < 1.0  # just committed


# ---- Read: dirty repo ----


class TestReadDirty:
    @pytest.mark.asyncio
    async def test_uncommitted_changes_detected(self, sensor, git_config):
        """Staged/unstaged changes surface as uncommitted_changes=True."""
        repo_path = str(Path(git_config.sensors.git.repos[0]).expanduser().resolve())
        sensor._repo_states[repo_path] = _RepoState()

        mock_fn = make_run_git_mock({
            "status": " M src/main.py\nM  src/config.py\n",
            "rev-list": "0\t0",
            "log": str(int(time.time())),
        })
        with patch.object(sensor, "_run_git", side_effect=mock_fn):
            result = await sensor.read()

        assert result["uncommitted_changes"] is True
        assert result["untracked_files"] == 0

    @pytest.mark.asyncio
    async def test_untracked_files_detected(self, sensor, git_config):
        """Untracked files are counted separately."""
        repo_path = str(Path(git_config.sensors.git.repos[0]).expanduser().resolve())
        sensor._repo_states[repo_path] = _RepoState()

        mock_fn = make_run_git_mock({
            "status": "?? new_file.py\n?? another.txt\n M existing.py\n",
            "rev-list": "0\t0",
            "log": str(int(time.time())),
        })
        with patch.object(sensor, "_run_git", side_effect=mock_fn):
            result = await sensor.read()

        assert result["uncommitted_changes"] is True  # the modified file
        assert result["untracked_files"] == 2

    @pytest.mark.asyncio
    async def test_workspace_sayr_thoughts_do_not_count_for_pressure(self, tmp_path):
        """Generated Sayr thoughts remain visible but do not wake workspace_git."""
        config = PulseConfig()
        repo_dir = tmp_path / "workspace"
        repo_dir.mkdir()
        config.sensors.git = GitSensorConfig(
            enabled=True,
            repos=[{"path": str(repo_dir), "name": "workspace", "drives": ["workspace_git"]}],
        )
        sensor = GitSensor(config)
        repo_path = str(repo_dir.resolve())
        sensor._repo_states[repo_path] = _RepoState()
        sensor._repo_meta[repo_path] = config.sensors.git.repos[0]

        mock_fn = make_run_git_mock({
            "status": "?? memory/sayr-thoughts/quiet.md\n",
            "rev-list": "0\t0",
            "log": str(int(time.time())),
        })
        with patch.object(sensor, "_run_git", side_effect=mock_fn):
            result = await sensor.read()

        repo = result["repos"][0]
        assert repo["untracked_files"] == 1
        assert repo["ignored_pressure_files"] == 1
        assert repo["pressure_untracked_files"] == 0
        assert repo["pressure_dirty"] is False
        assert result["untracked_files"] == 1
        assert result["pressure_dirty"] is False


# ---- Read: ahead/behind ----


class TestAheadBehind:
    @pytest.mark.asyncio
    async def test_commits_ahead(self, sensor, git_config):
        """Detects when local has commits not pushed to remote."""
        repo_path = str(Path(git_config.sensors.git.repos[0]).expanduser().resolve())
        sensor._repo_states[repo_path] = _RepoState()

        mock_fn = make_run_git_mock({
            "status": "",
            "rev-list": "3\t0",
            "log": str(int(time.time())),
        })
        with patch.object(sensor, "_run_git", side_effect=mock_fn):
            result = await sensor.read()

        assert result["commits_ahead"] == 3
        assert result["commits_behind"] == 0

    @pytest.mark.asyncio
    async def test_commits_behind(self, sensor, git_config):
        """Detects when remote has commits we haven't pulled."""
        repo_path = str(Path(git_config.sensors.git.repos[0]).expanduser().resolve())
        sensor._repo_states[repo_path] = _RepoState()

        mock_fn = make_run_git_mock({
            "status": "",
            "rev-list": "0\t5",
            "log": str(int(time.time())),
        })
        with patch.object(sensor, "_run_git", side_effect=mock_fn):
            result = await sensor.read()

        assert result["commits_ahead"] == 0
        assert result["commits_behind"] == 5

    @pytest.mark.asyncio
    async def test_no_upstream_returns_zero(self, sensor, git_config):
        """If no upstream is set, rev-list fails → 0/0."""
        repo_path = str(Path(git_config.sensors.git.repos[0]).expanduser().resolve())
        sensor._repo_states[repo_path] = _RepoState()

        async def mock_fn(args, cwd):
            if args[0] == "rev-parse":
                return ".git"
            if args[0] == "status":
                return ""
            if args[0] == "rev-list":
                return None  # fails — no upstream
            if args[0] == "log":
                return str(int(time.time()))
            return None

        with patch.object(sensor, "_run_git", side_effect=mock_fn):
            result = await sensor.read()

        assert result["commits_ahead"] == 0
        assert result["commits_behind"] == 0


# ---- Stale push detection ----


class TestStalePush:
    @pytest.mark.asyncio
    async def test_fresh_ahead_not_stale(self, sensor, git_config):
        """Commits ahead for less than threshold are not stale."""
        repo_path = str(Path(git_config.sensors.git.repos[0]).expanduser().resolve())
        sensor._repo_states[repo_path] = _RepoState()

        mock_fn = make_run_git_mock({
            "status": "",
            "rev-list": "2\t0",
            "log": str(int(time.time())),
        })
        with patch.object(sensor, "_run_git", side_effect=mock_fn):
            result = await sensor.read()

        assert result["commits_ahead"] == 2
        assert result["stale_push"] is False

    @pytest.mark.asyncio
    async def test_old_ahead_becomes_stale(self, sensor, git_config):
        """Commits ahead for longer than threshold trigger stale_push."""
        repo_path = str(Path(git_config.sensors.git.repos[0]).expanduser().resolve())
        # Simulate having first seen ahead 2 hours ago
        sensor._repo_states[repo_path] = _RepoState(
            first_seen_ahead_ts=time.time() - 7200  # 2 hours ago
        )

        mock_fn = make_run_git_mock({
            "status": "",
            "rev-list": "2\t0",
            "log": str(int(time.time())),
        })
        with patch.object(sensor, "_run_git", side_effect=mock_fn):
            result = await sensor.read()

        assert result["stale_push"] is True

    @pytest.mark.asyncio
    async def test_stale_clears_when_pushed(self, sensor, git_config):
        """Once pushed (ahead=0), stale_push clears and first_seen resets."""
        repo_path = str(Path(git_config.sensors.git.repos[0]).expanduser().resolve())
        sensor._repo_states[repo_path] = _RepoState(
            first_seen_ahead_ts=time.time() - 7200
        )

        mock_fn = make_run_git_mock({
            "status": "",
            "rev-list": "0\t0",  # pushed!
            "log": str(int(time.time())),
        })
        with patch.object(sensor, "_run_git", side_effect=mock_fn):
            result = await sensor.read()

        assert result["stale_push"] is False
        assert sensor._repo_states[repo_path].first_seen_ahead_ts is None


# ---- Last commit age ----


class TestLastCommit:
    @pytest.mark.asyncio
    async def test_recent_commit_age(self, sensor, git_config):
        """Last commit shows correct age in minutes."""
        repo_path = str(Path(git_config.sensors.git.repos[0]).expanduser().resolve())
        sensor._repo_states[repo_path] = _RepoState()

        ten_min_ago = str(int(time.time() - 600))
        mock_fn = make_run_git_mock({
            "status": "",
            "rev-list": "0\t0",
            "log": ten_min_ago,
        })
        with patch.object(sensor, "_run_git", side_effect=mock_fn):
            result = await sensor.read()

        assert result["last_commit_minutes_ago"] is not None
        assert 9.5 < result["last_commit_minutes_ago"] < 11.0

    @pytest.mark.asyncio
    async def test_no_commits_returns_none(self, sensor, git_config):
        """Empty repo with no commits returns None for last_commit_minutes_ago."""
        repo_path = str(Path(git_config.sensors.git.repos[0]).expanduser().resolve())
        sensor._repo_states[repo_path] = _RepoState()

        async def mock_fn(args, cwd):
            if args[0] == "rev-parse":
                return ".git"
            if args[0] == "log":
                return None  # no commits
            if args[0] == "status":
                return ""
            if args[0] == "rev-list":
                return None
            return None

        with patch.object(sensor, "_run_git", side_effect=mock_fn):
            result = await sensor.read()

        assert result["last_commit_minutes_ago"] is None


# ---- Not a git repo ----


class TestNotARepo:
    @pytest.mark.asyncio
    async def test_non_repo_returns_empty(self, sensor, git_config):
        """If rev-parse fails, returns zeroed result for that repo."""
        repo_path = str(Path(git_config.sensors.git.repos[0]).expanduser().resolve())
        sensor._repo_states[repo_path] = _RepoState()

        async def mock_fn(args, cwd):
            if args[0] == "rev-parse":
                return None  # not a repo
            return None

        with patch.object(sensor, "_run_git", side_effect=mock_fn):
            result = await sensor.read()

        assert result["uncommitted_changes"] is False
        assert result["untracked_files"] == 0
        assert result["commits_ahead"] == 0


# ---- Empty state (no repos configured) ----


class TestEmptyState:
    @pytest.mark.asyncio
    async def test_no_repos_returns_empty(self):
        """Sensor with no repos returns clean empty result."""
        config = PulseConfig()
        config.sensors.git = GitSensorConfig(enabled=True, repos=[])
        s = GitSensor(config)
        result = await s.read()
        assert result["repos"] == []
        assert result["uncommitted_changes"] is False


# ---- Multi-repo aggregation ----


class TestMultiRepo:
    @pytest.mark.asyncio
    async def test_aggregates_across_repos(self, tmp_path):
        """Results aggregate across multiple configured repos."""
        config = PulseConfig()
        repo1 = tmp_path / "repo1"
        repo2 = tmp_path / "repo2"
        repo1.mkdir()
        repo2.mkdir()
        config.sensors.git = GitSensorConfig(
            enabled=True,
            repos=[str(repo1), str(repo2)],
        )
        s = GitSensor(config)
        r1 = str(repo1.resolve())
        r2 = str(repo2.resolve())
        s._repo_states[r1] = _RepoState()
        s._repo_states[r2] = _RepoState()

        call_count = {}

        async def mock_fn(args, cwd):
            call_count[cwd] = call_count.get(cwd, 0) + 1
            if args[0] == "rev-parse":
                return ".git"
            if args[0] == "status":
                if cwd == r1:
                    return "?? stray.txt\n"
                return " M dirty.py\n"
            if args[0] == "rev-list":
                if cwd == r1:
                    return "1\t0"
                return "0\t2"
            if args[0] == "log":
                return str(int(time.time()))
            return None

        with patch.object(s, "_run_git", side_effect=mock_fn):
            result = await s.read()

        assert len(result["repos"]) == 2
        # repo1: 1 untracked, repo2: 1 modified
        assert result["untracked_files"] == 1
        assert result["uncommitted_changes"] is True  # repo2 has modified
        assert result["pressure_untracked_files"] == 1
        assert result["pressure_uncommitted_changes"] == 1
        assert result["pressure_dirty"] is True
        assert result["commits_ahead"] == 1  # repo1
        assert result["commits_behind"] == 2  # repo2


# ---- Drive engine integration ----


class TestDriveEngineIntegration:
    def _make_engine(self):
        config = PulseConfig()
        td = tempfile.mkdtemp()
        config.state.dir = td
        sp = StatePersistence(config)
        engine = DriveEngine(config, sp)
        # Inject drives needed for git sensor spikes
        engine.drives["goals"] = Drive(name="goals", category="goals", weight=1.0)
        engine.drives["growth"] = Drive(name="growth", category="growth", weight=1.0)
        return engine

    def test_uncommitted_spikes_goals(self):
        """Git uncommitted_changes feeds goals drive spike."""
        engine = self._make_engine()
        initial = engine.drives["goals"].pressure

        sensor_data = {
            "git": {
                "uncommitted_changes": True,
                "untracked_files": 0,
                "stale_push": False,
                "commits_behind": 0,
            }
        }
        engine._apply_sensor_spikes(sensor_data)
        assert engine.drives["goals"].pressure > initial

    def test_stale_push_spikes_goals_harder(self):
        """Git stale_push fires a larger goals spike than uncommitted."""
        sensor_dirty = {
            "git": {
                "uncommitted_changes": True,
                "untracked_files": 0,
                "stale_push": False,
                "commits_behind": 0,
            }
        }
        sensor_stale = {
            "git": {
                "uncommitted_changes": False,
                "untracked_files": 0,
                "stale_push": True,
                "commits_behind": 0,
            }
        }
        e1 = self._make_engine()
        e1._apply_sensor_spikes(sensor_dirty)
        dirty_pressure = e1.drives["goals"].pressure

        e2 = self._make_engine()
        e2._apply_sensor_spikes(sensor_stale)
        stale_pressure = e2.drives["goals"].pressure

        assert stale_pressure > dirty_pressure

    def test_behind_spikes_growth(self):
        """Git commits_behind fires growth drive spike."""
        engine = self._make_engine()
        initial_growth = engine.drives["growth"].pressure

        sensor_data = {
            "git": {
                "uncommitted_changes": False,
                "untracked_files": 0,
                "stale_push": False,
                "commits_behind": 3,
            }
        }
        engine._apply_sensor_spikes(sensor_data)
        assert engine.drives["growth"].pressure > initial_growth

    def test_untracked_files_spike_goals(self):
        """Untracked files also spike goals drive for legacy aggregate sensor data."""
        engine = self._make_engine()
        initial = engine.drives["goals"].pressure

        sensor_data = {
            "git": {
                "uncommitted_changes": False,
                "untracked_files": 3,
                "stale_push": False,
                "commits_behind": 0,
            }
        }
        engine._apply_sensor_spikes(sensor_data)
        assert engine.drives["goals"].pressure > initial

    def test_repo_specific_dirty_spikes_declared_drive(self):
        """Per-repo git dirtiness feeds the repo's declared drive."""
        engine = self._make_engine()
        engine.drives["workspace_git"] = Drive(name="workspace_git", category="workspace_git", weight=1.0)
        initial_workspace = engine.drives["workspace_git"].pressure
        initial_goals = engine.drives["goals"].pressure

        sensor_data = {
            "git": {
                "repos": [
                    {
                        "drives": ["workspace_git"],
                        "pressure_dirty": True,
                        "stale_push": False,
                        "commits_behind": 0,
                    }
                ],
            }
        }
        engine._apply_sensor_spikes(sensor_data)
        assert engine.drives["workspace_git"].pressure > initial_workspace
        assert engine.drives["goals"].pressure == initial_goals

    def test_repo_ignored_pressure_does_not_spike_declared_drive(self):
        """Repos dirty only in ignored files do not wake their git drive."""
        engine = self._make_engine()
        engine.drives["workspace_git"] = Drive(name="workspace_git", category="workspace_git", weight=1.0)
        initial_workspace = engine.drives["workspace_git"].pressure

        sensor_data = {
            "git": {
                "repos": [
                    {
                        "drives": ["workspace_git"],
                        "pressure_dirty": False,
                        "untracked_files": 1,
                        "ignored_pressure_files": 1,
                        "stale_push": False,
                        "commits_behind": 0,
                    }
                ],
            }
        }
        engine._apply_sensor_spikes(sensor_data)
        assert engine.drives["workspace_git"].pressure == initial_workspace

    def test_git_drive_does_not_accumulate_time_pressure(self):
        """Repo-local git drives are event driven, not time-accumulating."""
        engine = self._make_engine()
        engine.drives["pulse_git"] = Drive(name="pulse_git", category="pulse_git", weight=1.0)
        engine.last_tick_time -= 3600

        engine.tick({"git": {"repos": []}})

        assert engine.drives["pulse_git"].pressure == 0.0

    def test_clean_repo_clears_stale_git_pressure(self):
        """A clean repo snapshot discharges old git pressure for that repo."""
        engine = self._make_engine()
        engine.drives["pulse_git"] = Drive(name="pulse_git", category="pulse_git", pressure=3.0, weight=1.0)

        sensor_data = {
            "git": {
                "repos": [
                    {
                        "drives": ["pulse_git"],
                        "pressure_dirty": False,
                        "stale_push": False,
                        "commits_behind": 0,
                    }
                ],
            }
        }

        engine._apply_sensor_spikes(sensor_data)

        assert engine.drives["pulse_git"].pressure == 0.0

    def test_dirty_repo_preserves_and_spikes_git_pressure(self):
        """Dirty repo snapshots still raise the declared git drive."""
        engine = self._make_engine()
        engine.drives["pulse_git"] = Drive(name="pulse_git", category="pulse_git", pressure=0.2, weight=1.0)

        sensor_data = {
            "git": {
                "repos": [
                    {
                        "drives": ["pulse_git"],
                        "pressure_dirty": True,
                        "stale_push": False,
                        "commits_behind": 0,
                    }
                ],
            }
        }

        engine._apply_sensor_spikes(sensor_data)

        assert engine.drives["pulse_git"].pressure > 0.2


# ---- Sensor manager registration ----


class TestSensorManagerRegistration:
    def test_git_sensor_registers_when_enabled(self, tmp_path):
        """SensorManager includes GitSensor when enabled + repos configured."""
        from pulse.src.sensors.manager import SensorManager
        config = PulseConfig()
        repo = tmp_path / "repo"
        repo.mkdir()
        config.sensors.git = GitSensorConfig(
            enabled=True,
            repos=[str(repo)],
        )
        # SensorManager.__init__ needs filesystem + watchdog, mock it
        with patch("pulse.src.sensors.manager.Observer"), \
             patch("pulse.src.sensors.manager._WatchdogHandler"):
            mgr = SensorManager(config)
        sensor_names = [s.name for s in mgr.sensors]
        assert "git" in sensor_names

    def test_git_sensor_skipped_when_disabled(self):
        """GitSensor not registered when disabled."""
        from pulse.src.sensors.manager import SensorManager
        config = PulseConfig()
        config.sensors.git = GitSensorConfig(enabled=False)
        with patch("pulse.src.sensors.manager.Observer"), \
             patch("pulse.src.sensors.manager._WatchdogHandler"):
            mgr = SensorManager(config)
        sensor_names = [s.name for s in mgr.sensors]
        assert "git" not in sensor_names
