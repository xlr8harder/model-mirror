import hashlib
import json
import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace

import libtorrent as lt
import pytest

from model_mirror.checksums import load_manifest, write_checksums
from model_mirror.config import Config
from model_mirror.hub import HubFile, HubSnapshot, write_snapshot_plan
from model_mirror.state import VerificationState, read_verification_state, write_verification_state
from model_mirror.torrent import DESCRIPTOR_INFO_KEY, TorrentPublicationError
from model_mirror.torrent_coverage import load_coverage
from model_mirror.torrent_import import (
    MAX_TORRENT_PAYLOAD_BYTES,
    bytes_text,
    coverage_from_metainfo,
    external_handoff,
    import_external_payload,
    join_torrent,
    magnet_staging_key,
    metainfo_from_handle,
    parse_descriptor,
    parse_publication_metainfo,
    remove_padding_files,
    shell_quote,
    staging_parent,
    validate_path_collisions,
    validate_staged_payload,
    validate_v2_file_tree,
)
from model_mirror.torrent_publication import (
    create_publication,
    load_fenced_publication,
    retire_publication,
    reusable_imported_publication,
)


COMMIT = "a" * 40


def git_blob(payload):
    return hashlib.sha1(f"blob {len(payload)}\0".encode() + payload).hexdigest()


def source_publication(tmp_path):
    root = tmp_path / "source" / "models" / "org" / "model"
    root.mkdir(parents=True)
    payloads = {
        "README.md": b"hello",
        "empty.txt": b"",
        "nested/weights.bin": bytes(range(251)) * 5000,
    }
    files = []
    for rel, payload in payloads.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        if rel.endswith(".bin"):
            files.append(HubFile(rel, len(payload), lfs_sha256=hashlib.sha256(payload).hexdigest()))
        else:
            files.append(HubFile(rel, len(payload), blob_id=git_blob(payload)))
    write_checksums(root)
    write_snapshot_plan(root, HubSnapshot("org/model", "model", "main", COMMIT, files))
    write_verification_state(
        root,
        VerificationState("clean", "org/model", resolved_commit=COMMIT, upstream_commit=COMMIT),
    )
    publication = create_publication(root, repo_id="org/model", repo_type="model")
    return root, payloads, publication


def stage_payload(base, parsed, payloads, *, add_padding=False):
    root = base / parsed.root_name
    root.mkdir(parents=True)
    for rel, payload in payloads.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    if add_padding:
        info = parsed.metainfo_tree[b"info"]
        for row in info[b"files"]:
            if row.get(b"attr") == b"p":
                path = root.joinpath(*(part.decode() for part in row[b"path"]))
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(bytes(row[b"length"]))
    return root


def test_parse_handoff_and_external_import_finalize_normal_archive(tmp_path):
    source, payloads, publication = source_publication(tmp_path)
    metainfo = publication.torrent_path.read_bytes()
    parsed = parse_publication_metainfo(metainfo)
    assert parsed.descriptor.repo_id == "org/model"
    assert parsed.artifact.infohash_v2 == publication.record.infohash_v2

    config = Config(directory=tmp_path / "destination")
    parent, command = external_handoff(config, parsed, publication.torrent_path)
    assert parent == staging_parent(config, parsed.artifact.infohash_v2)
    assert str(parent / "model") in command
    assert shell_quote("a b") == "'a b'"

    payload_root = stage_payload(parent, parsed, payloads, add_padding=True)
    result = import_external_payload(
        config,
        metainfo=metainfo,
        payload_root=payload_root,
    )
    assert result.path == config.directory / "models" / "org" / "model"
    assert result.reread_files == 3 and result.reread_bytes == sum(map(len, payloads.values()))
    assert not (result.path / ".pad").exists()
    assert load_manifest(result.path)["README.md"]["sha256"] == hashlib.sha256(b"hello").hexdigest()
    state = read_verification_state(result.path)
    assert state.status == "torrent-verified" and state.upstream_status == "unknown"
    record = load_fenced_publication(result.path)[0]
    assert record.content_verification == "torrent-verified"
    assert record.publication_trust == "trusted-infohash"
    assert not record.desired_seed
    coverage = load_coverage(
        result.path
        / ".model-mirror"
        / "torrent"
        / "coverage"
        / f"hybrid-v1-v2-1--{COMMIT}.json"
    )
    assert set(coverage.files) == set(payloads)
    with pytest.raises(TorrentPublicationError, match="already exists"):
        import_external_payload(config, metainfo=metainfo, payload_root=result.path)

    retire_publication(result.path)
    reused = create_publication(
        result.path,
        repo_id="org/model",
        repo_type="model",
        desired_seed=True,
    )
    assert reused.created and reused.record.desired_seed
    preserved = create_publication(
        result.path,
        repo_id="org/model",
        repo_type="model",
    )
    assert not preserved.created and preserved.record.desired_seed
    external = create_publication(
        result.path,
        repo_id="org/model",
        repo_type="model",
        desired_seed=False,
        client_mode="external",
    )
    assert external.record.client_mode == "external"
    assert external.record.observed_backend == "external"


def test_backend_verified_import_reconstructs_coverage_without_payload_reread(tmp_path):
    _source, payloads, publication = source_publication(tmp_path)
    parsed = parse_publication_metainfo(publication.torrent_path.read_bytes())
    config = Config(directory=tmp_path / "destination")
    payload_root = stage_payload(tmp_path / "staging", parsed, payloads)

    result = import_external_payload(
        config,
        metainfo=publication.torrent_path.read_bytes(),
        payload_root=payload_root,
        backend_verified=True,
        seed=True,
    )

    assert result.reread_files == 0 and result.reread_bytes == 0
    assert result.publication.desired_seed
    coverage = coverage_from_metainfo(result.path, parsed)
    assert coverage.files["empty.txt"].v2_file_root is None
    assert coverage.files["README.md"].v2_file_root is not None

    assert reusable_imported_publication(
        tmp_path / "missing",
        repo_id="org/model",
        repo_type="model",
        desired_seed=None,
        client_mode=None,
    ) is None
    assert reusable_imported_publication(
        result.path,
        repo_id="wrong/model",
        repo_type="model",
        desired_seed=None,
        client_mode=None,
    ) is None

    record = load_fenced_publication(result.path)[0]
    torrent_path = result.path / record.torrent_path
    original = torrent_path.read_bytes()
    torrent_path.unlink()
    with pytest.raises(TorrentPublicationError, match="unavailable"):
        create_publication(result.path, repo_id="org/model", repo_type="model")
    torrent_path.write_bytes(b"corrupt")
    with pytest.raises(TorrentPublicationError, match="digest mismatch"):
        create_publication(result.path, repo_id="org/model", repo_type="model")
    torrent_path.write_bytes(original)
    (result.path / "README.md").write_bytes(b"changed")
    with pytest.raises(TorrentPublicationError, match="cannot reuse"):
        create_publication(result.path, repo_id="org/model", repo_type="model")


def test_staged_payload_rejects_wrong_names_files_links_sizes_and_padding(tmp_path):
    _source, payloads, publication = source_publication(tmp_path)
    parsed = parse_publication_metainfo(publication.torrent_path.read_bytes())
    descriptor = parsed.descriptor
    with pytest.raises(TorrentPublicationError, match="missing or unsafe"):
        validate_staged_payload(tmp_path / "does-not-exist", descriptor)

    wrong_name = stage_payload(tmp_path / "wrong", parsed, payloads)
    renamed = wrong_name.rename(wrong_name.with_name("not-model"))
    with pytest.raises(TorrentPublicationError, match="must be named"):
        import_external_payload(
            Config(directory=tmp_path / "dest-name"),
            metainfo=publication.torrent_path.read_bytes(),
            payload_root=renamed,
        )

    cases = []
    missing = stage_payload(tmp_path / "missing", parsed, payloads)
    (missing / "README.md").unlink()
    cases.append((missing, "missing"))
    extra = stage_payload(tmp_path / "extra", parsed, payloads)
    (extra / "surprise").write_text("x")
    cases.append((extra, "unexpected"))
    wrong_size = stage_payload(tmp_path / "size", parsed, payloads)
    (wrong_size / "README.md").write_text("wrong-size")
    cases.append((wrong_size, "size mismatch"))
    symlinked = stage_payload(tmp_path / "link", parsed, payloads)
    (symlinked / "README.md").unlink()
    (symlinked / "README.md").symlink_to("empty.txt")
    cases.append((symlinked, "symlink"))
    for root, message in cases:
        with pytest.raises(TorrentPublicationError, match=message):
            validate_staged_payload(root, descriptor)

    unsafe_pad = stage_payload(tmp_path / "pad", parsed, payloads)
    (unsafe_pad / ".pad").write_text("not-directory")
    with pytest.raises(TorrentPublicationError, match="padding path"):
        remove_padding_files(unsafe_pad, descriptor)

    bad_pad = stage_payload(tmp_path / "bad-pad", parsed, payloads)
    (bad_pad / ".pad").mkdir()
    (bad_pad / ".pad" / "unexpected").write_text("x")
    with pytest.raises(TorrentPublicationError, match="unexpected torrent padding"):
        remove_padding_files(bad_pad, descriptor)

    absent_pad = stage_payload(tmp_path / "no-pad", parsed, payloads)
    remove_padding_files(absent_pad, descriptor)


def test_import_refuses_cross_filesystem_finalization(tmp_path):
    shared_memory = Path("/dev/shm")
    if not shared_memory.is_dir() or shared_memory.stat().st_dev == tmp_path.stat().st_dev:
        pytest.skip("no distinct temporary filesystem is available")
    _source, payloads, publication = source_publication(tmp_path)
    parsed = parse_publication_metainfo(publication.torrent_path.read_bytes())
    with tempfile.TemporaryDirectory(dir=shared_memory) as temporary:
        payload_root = stage_payload(Path(temporary), parsed, payloads)
        with pytest.raises(TorrentPublicationError, match="different filesystems"):
            import_external_payload(
                Config(directory=tmp_path / "destination"),
                metainfo=publication.torrent_path.read_bytes(),
                payload_root=payload_root,
            )


def test_descriptor_and_metainfo_hostile_inputs_are_rejected(tmp_path):
    _source, _payloads, publication = source_publication(tmp_path)
    metainfo = publication.torrent_path.read_bytes()
    tree = lt.bdecode(metainfo)
    descriptor = tree[b"info"][DESCRIPTOR_INFO_KEY]

    with pytest.raises(TorrentPublicationError, match="malformed"):
        parse_publication_metainfo(b"not-bencode")
    for bad_tree, message in (
        ({}, "no info"),
        ({b"info": {}}, "does not contain"),
        ({b"info": {DESCRIPTOR_INFO_KEY: b"x"}}, "does not contain"),
    ):
        with pytest.raises(TorrentPublicationError, match=message):
            parse_publication_metainfo(bytes(lt.bencode(bad_tree)))

    descriptor_cases = [
        ({**descriptor, b"schema": b"other"}, "schema"),
        ({**descriptor, b"version": 2}, "version"),
        ({**descriptor, b"profile": b"other"}, "profile"),
        ({**descriptor, b"provider": b"other"}, "provider"),
        ({key: value for key, value in descriptor.items() if key != b"repo_id"}, "malformed"),
        ({**descriptor, b"repo_id": b"../bad"}, "Invalid repository"),
        ({**descriptor, b"repo_type": b"other"}, "repository type"),
        ({**descriptor, b"resolved_commit": b"../bad"}, "resolved commit"),
        ({**descriptor, b"files": []}, "file count"),
        ({**descriptor, b"files": [b"bad"]}, "file row"),
    ]
    for value, message in descriptor_cases:
        with pytest.raises((TorrentPublicationError, ValueError), match=message):
            parse_descriptor(value)

    row = descriptor[b"files"][0]
    row_cases = [
        {**row, b"unknown": b"x"},
        {key: value for key, value in row.items() if key != b"path"},
        {**row, b"size": -1},
        {**row, b"sha256": b"bad"},
        {**row, b"lfs_sha256": b"0" * 64},
        {**row, b"blob_id": b"0" * 40},
        {key: value for key, value in row.items() if key not in {b"blob_id", b"lfs_sha256"}},
    ]
    for bad_row in row_cases:
        with pytest.raises(TorrentPublicationError):
            parse_descriptor({**descriptor, b"files": [bad_row]})

    duplicate = {**descriptor, b"files": [row, row]}
    with pytest.raises(TorrentPublicationError, match="duplicate"):
        parse_descriptor(duplicate)
    collision_parent = {
        **row,
        b"path": b"nested",
        b"size": 1,
        b"sha256": hashlib.sha256(b"x").hexdigest().encode(),
        b"git_blob_sha1": git_blob(b"x").encode(),
        b"blob_id": git_blob(b"x").encode(),
    }
    collision_child = {**collision_parent, b"path": b"nested/file"}
    with pytest.raises(TorrentPublicationError, match="collision"):
        parse_descriptor({**descriptor, b"files": [collision_parent, collision_child]})
    with pytest.raises(TorrentPublicationError, match="collision"):
        validate_path_collisions({"a", "a/b"})
    huge = {**row, b"size": MAX_TORRENT_PAYLOAD_BYTES + 1}
    with pytest.raises(TorrentPublicationError, match="payload size"):
        parse_descriptor({**descriptor, b"files": [huge]})
    zero = {**row, b"size": 0}
    with pytest.raises(TorrentPublicationError, match="piece length"):
        parse_descriptor({**descriptor, b"files": [zero]})
    with pytest.raises(TorrentPublicationError, match="piece length"):
        parse_descriptor({**descriptor, b"piece_length": 2 * 1024 * 1024})

    for mutate, message in (
        (lambda value: value[b"info"].__setitem__(b"piece length", 123), "piece length"),
        (lambda value: value[b"info"].__setitem__(b"meta version", 1), "v2"),
        (lambda value: value[b"info"].pop(b"name"), "root name"),
        (lambda value: value[b"info"].__setitem__(b"name", b"wrong"), "root name"),
        (lambda value: value[b"info"].__setitem__(b"files", []), "payload layout"),
        (lambda value: value[b"info"].__setitem__(b"file tree", {}), "file tree"),
    ):
        altered = lt.bdecode(metainfo)
        mutate(altered)
        with pytest.raises(TorrentPublicationError, match=message):
            parse_publication_metainfo(bytes(lt.bencode(altered)))

    altered = lt.bdecode(metainfo)
    altered[b"info"][DESCRIPTOR_INFO_KEY][b"extra"] = b"x"
    with pytest.raises(TorrentPublicationError, match="non-canonical"):
        parse_publication_metainfo(bytes(lt.bencode(altered)))

    # Same decoded tree, but deliberately non-canonical top-level dictionary order.
    canonical = lt.bdecode(metainfo)
    noncanonical = (
        b"d12:piece layers"
        + bytes(lt.bencode(canonical[b"piece layers"]))
        + b"4:info"
        + bytes(lt.bencode(canonical[b"info"]))
        + b"e"
    )
    with pytest.raises(TorrentPublicationError, match="canonically"):
        parse_publication_metainfo(noncanonical)


def test_v2_tree_text_and_coverage_validation_helpers(tmp_path):
    _source, payloads, publication = source_publication(tmp_path)
    parsed = parse_publication_metainfo(publication.torrent_path.read_bytes())
    descriptor = parsed.descriptor
    assert bytes_text(b"hello", "x") == "hello"
    with pytest.raises(TorrentPublicationError, match="byte string"):
        bytes_text("x", "label")
    with pytest.raises(TorrentPublicationError, match="UTF-8"):
        bytes_text(b"\xff", "label")

    for tree in (None, {b"": {}}, {b"x": b"bad"}, {b"x": {b"": {}, b"child": {}}}):
        with pytest.raises(TorrentPublicationError):
            validate_v2_file_tree(tree, descriptor)

    tree = lt.bdecode(publication.torrent_path.read_bytes())[b"info"][b"file tree"]
    bad_tree = lt.bdecode(bytes(lt.bencode(tree)))
    bad_tree[b"README.md"][b""][b"length"] = 99
    with pytest.raises(TorrentPublicationError, match="invalid v2 file leaf"):
        validate_v2_file_tree(bad_tree, descriptor)
    bad_tree = lt.bdecode(bytes(lt.bencode(tree)))
    bad_tree[b"empty.txt"][b""][b"pieces root"] = b"x" * 32
    with pytest.raises(TorrentPublicationError, match="empty file"):
        validate_v2_file_tree(bad_tree, descriptor)
    bad_tree = lt.bdecode(bytes(lt.bencode(tree)))
    bad_tree[b"README.md"][b""].pop(b"pieces root")
    with pytest.raises(TorrentPublicationError, match="no valid"):
        validate_v2_file_tree(bad_tree, descriptor)

    stage = stage_payload(tmp_path / "stage", parsed, payloads)
    bad = ParsedCopy(parsed, lt.bdecode(publication.torrent_path.read_bytes()))
    bad.metainfo_tree[b"info"][b"pieces"] = b"x"
    with pytest.raises(TorrentPublicationError, match="v1 piece"):
        coverage_from_metainfo(stage, bad)

    bad = ParsedCopy(parsed, lt.bdecode(publication.torrent_path.read_bytes()))
    bad.metainfo_tree[b"piece layers"] = []
    with pytest.raises(TorrentPublicationError, match="piece layers"):
        coverage_from_metainfo(stage, bad)

    bad = ParsedCopy(parsed, lt.bdecode(publication.torrent_path.read_bytes()))
    bad.metainfo_tree[b"info"][b"pieces"] = b""
    with pytest.raises(TorrentPublicationError, match="do not cover"):
        coverage_from_metainfo(stage, bad)

    bad = ParsedCopy(parsed, lt.bdecode(publication.torrent_path.read_bytes()))
    large_root = bad.metainfo_tree[b"info"][b"file tree"][b"nested"][b"weights.bin"][b""][b"pieces root"]
    bad.metainfo_tree[b"piece layers"].pop(large_root)
    with pytest.raises(TorrentPublicationError, match="piece layer"):
        coverage_from_metainfo(stage, bad)

    bad = ParsedCopy(parsed, lt.bdecode(publication.torrent_path.read_bytes()))
    bad.metainfo_tree[b"info"][b"pieces"] += b"x" * 20
    with pytest.raises(TorrentPublicationError, match="extra v1"):
        coverage_from_metainfo(stage, bad)


class ParsedCopy:
    def __init__(self, parsed, tree):
        self.descriptor = parsed.descriptor
        self.artifact = parsed.artifact
        self.metainfo_tree = tree
        self.root_name = parsed.root_name


class FakeStatus:
    def __init__(self, *, metadata=True, seeding=True, error=""):
        self.has_metadata = metadata
        self.is_seeding = seeding
        self.is_finished = seeding
        self.error = error
        self.progress = 1.0 if seeding else 0.0
        self.name = "model"
        self.download_payload_rate = 0
        self.num_peers = 0


class FakeHandle:
    def __init__(self, torrent_info, statuses):
        self._torrent_info = torrent_info
        self.statuses = list(statuses)

    def status(self):
        if len(self.statuses) > 1:
            return self.statuses.pop(0)
        return self.statuses[0]

    def torrent_file(self):
        return self._torrent_info


class FakeSession:
    def __init__(self, metainfo, payloads, statuses):
        self.metainfo = metainfo
        self.payloads = payloads
        self.statuses = statuses
        self.removed = []

    def add_torrent(self, params):
        root = Path(params.save_path) / "model"
        root.mkdir(parents=True, exist_ok=True)
        for rel, payload in self.payloads.items():
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
        return FakeHandle(lt.torrent_info(self.metainfo), self.statuses)

    def remove_torrent(self, handle):
        self.removed.append(handle)


def test_native_file_and_magnet_join_use_same_no_reread_finalizer(tmp_path):
    _source, payloads, publication = source_publication(tmp_path)
    metainfo = publication.torrent_path.read_bytes()
    hybrid_params = lt.parse_magnet_uri(publication.record.magnet_uri)
    assert magnet_staging_key(hybrid_params, publication.record.magnet_uri) == publication.record.infohash_v2
    v1_magnet = "magnet:?xt=urn:btih:" + "b" * 40
    assert magnet_staging_key(lt.parse_magnet_uri(v1_magnet), v1_magnet) == "b" * 40
    no_hashes = SimpleNamespace(
        info_hashes=SimpleNamespace(has_v2=lambda: False, has_v1=lambda: False)
    )
    assert magnet_staging_key(no_hashes, "magnet:?dn=x") == hashlib.sha256(b"magnet:?dn=x").hexdigest()
    statuses = [FakeStatus(seeding=True)]
    session = FakeSession(metainfo, payloads, statuses)
    config = Config(directory=tmp_path / "file-dest")
    progress = []
    result = join_torrent(
        config,
        str(publication.torrent_path),
        session=session,
        on_progress=lambda status: progress.append(status.progress),
    )
    assert result.reread_files == 0 and session.removed and progress == [1.0]

    session = FakeSession(
        metainfo,
        payloads,
        [FakeStatus(metadata=False, seeding=False), FakeStatus(metadata=True, seeding=True)],
    )
    config = Config(directory=tmp_path / "magnet-dest")
    result = join_torrent(
        config,
        publication.record.magnet_uri,
        session=session,
        poll_seconds=0.001,
    )
    assert result.path.exists() and result.reread_files == 0

    session = FakeSession(
        metainfo,
        payloads,
        [FakeStatus(seeding=False), FakeStatus(seeding=True)],
    )
    result = join_torrent(
        Config(directory=tmp_path / "waiting-dest"),
        str(publication.torrent_path),
        session=session,
        poll_seconds=0.001,
    )
    assert result.path.exists()


def test_native_join_reports_source_timeout_and_backend_errors(tmp_path):
    _source, payloads, publication = source_publication(tmp_path)
    with pytest.raises(TorrentPublicationError, match="not a file or magnet"):
        join_torrent(Config(directory=tmp_path / "x"), "nope", session=object())
    with pytest.raises(ValueError, match="positive"):
        join_torrent(Config(directory=tmp_path / "x"), "nope", poll_seconds=0)

    error_session = FakeSession(
        publication.torrent_path.read_bytes(),
        payloads,
        [FakeStatus(error="boom")],
    )
    with pytest.raises(TorrentPublicationError, match="download failed"):
        join_torrent(
            Config(directory=tmp_path / "error"),
            str(publication.torrent_path),
            session=error_session,
        )

    timeout_session = FakeSession(
        publication.torrent_path.read_bytes(),
        payloads,
        [FakeStatus(metadata=False, seeding=False)],
    )
    with pytest.raises(TorrentPublicationError, match="timed out"):
        join_torrent(
            Config(directory=tmp_path / "timeout"),
            publication.record.magnet_uri,
            session=timeout_session,
            metadata_timeout_seconds=0.001,
            poll_seconds=0.001,
        )

    metadata_error_session = FakeSession(
        publication.torrent_path.read_bytes(),
        payloads,
        [FakeStatus(metadata=False, seeding=False, error="metadata boom")],
    )
    with pytest.raises(TorrentPublicationError, match="metadata acquisition failed"):
        join_torrent(
            Config(directory=tmp_path / "metadata-error"),
            publication.record.magnet_uri,
            session=metadata_error_session,
        )


def test_metainfo_from_handle_rejects_malformed_backend_metadata(tmp_path):
    _source, _payloads, publication = source_publication(tmp_path)
    handle = FakeHandle(lt.torrent_info(publication.torrent_path.read_bytes()), [FakeStatus()])
    rebuilt = metainfo_from_handle(handle, lt)
    assert parse_publication_metainfo(rebuilt).artifact.infohash_v2 == publication.record.infohash_v2

    class BadCreator:
        def generate(self):
            return {b"creation date": 1, b"info": b"bad"}

    fake_lt = SimpleNamespace(create_torrent=lambda info: BadCreator())
    with pytest.raises(TorrentPublicationError, match="malformed metadata"):
        metainfo_from_handle(handle, fake_lt)
