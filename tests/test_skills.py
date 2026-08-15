"""Installing, replacing, downloading and removing skills."""

import io
import tarfile

import pytest
from conftest import tar_skill


def install(client, archive, query=""):
    return client.request("POST", f"/skills{query}", body=archive)


@pytest.fixture(autouse=True)
def no_skills_left_behind(addon):
    """Each test starts with an empty skills directory and leaves one behind."""
    for existing in addon.SKILLS_DIR.iterdir():
        if existing.is_dir():
            addon.delete_skill(existing.name) if not existing.name.startswith(".") else None
    yield


def test_a_skill_is_named_by_its_own_skill_md(client):
    answer = install(client, tar_skill(name="from-frontmatter"))

    assert answer.status == 201
    assert answer.json["name"] == "from-frontmatter"
    assert [skill["name"] for skill in client.get("/skills").json["skills"]] == [
        "from-frontmatter"
    ]


def test_a_folded_description_is_read_as_one_line(client):
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w:gz") as writer:
        body = b"---\nname: folded\ndescription: >-\n  folded over\n  two lines\n---\n\n# hi\n"
        entry = tarfile.TarInfo("wrapper/SKILL.md")
        entry.size = len(body)
        writer.addfile(entry, io.BytesIO(body))

    answer = install(client, archive.getvalue())

    assert answer.json["description"] == "folded over two lines"


def test_a_skill_with_no_name_anywhere_says_what_to_do_about_it(client):
    flat = io.BytesIO()
    with tarfile.open(fileobj=flat, mode="w:gz") as writer:
        body = b"---\ndescription: nameless\n---\n"
        entry = tarfile.TarInfo("SKILL.md")
        entry.size = len(body)
        writer.addfile(entry, io.BytesIO(body))

    answer = install(client, flat.getvalue())

    assert answer.status == 400
    assert "?name=" in answer.json["error"]


def test_the_wrapping_folder_names_a_skill_whose_frontmatter_does_not(client):
    answer = install(client, tar_skill())

    assert answer.status == 201
    assert answer.json["name"] == "wrapper"


def test_an_explicitly_given_name_wins_over_the_frontmatter(client):
    answer = install(client, tar_skill(name="ignored"), query="?name=chosen-by-caller")

    assert answer.json["name"] == "chosen-by-caller"


def test_a_name_that_could_escape_the_skills_directory_is_refused(client):
    answer = install(client, tar_skill(name="fine"), query="?name=../escaped")

    assert answer.status == 400
    assert "unsafe name" in answer.json["error"]


def test_a_frontmatter_name_that_is_not_usable_is_refused(client):
    answer = install(client, tar_skill(name="../escaped"))

    assert answer.status == 400
    assert "not usable" in answer.json["error"]


def test_installing_over_an_existing_skill_replaces_it(addon, client):
    install(client, tar_skill(name="replaced", extra_files={"old.txt": "gone"}))

    install(client, tar_skill(name="replaced", extra_files={"new.txt": "here"}))

    installed = addon.SKILLS_DIR / "replaced"
    assert (installed / "new.txt").is_file()
    assert not (installed / "old.txt").exists()


def test_a_symlink_where_the_skill_should_go_is_replaced_not_written_through(addon, client):
    outside = addon.DATA / "outside-the-skills-directory"
    outside.mkdir(exist_ok=True)
    link = addon.SKILLS_DIR / "linked"
    link.symlink_to(outside)

    install(client, tar_skill(name="linked"))

    assert link.is_dir()
    assert not link.is_symlink()
    assert not (outside / "SKILL.md").exists()


def test_an_archive_holding_more_entries_than_allowed_is_refused(addon, client):
    crowded = io.BytesIO()
    with tarfile.open(fileobj=crowded, mode="w:gz", compresslevel=1) as writer:
        for index in range(addon.MAX_MEMBERS + 5):
            writer.addfile(tarfile.TarInfo(f"skill/file{index}"), io.BytesIO(b""))

    answer = install(client, crowded.getvalue())

    assert answer.status == 413
    assert "entries" in answer.json["error"]


def test_an_archive_that_expands_to_fill_the_disk_is_refused(addon, client):
    """A zero-filled tar gzips at roughly 1000:1, so the upload cap says nothing."""

    class Zeros(io.RawIOBase):
        def __init__(self, count):
            self.left = count

        def readable(self):
            return True

        def readinto(self, buffer):
            size = min(len(buffer), self.left)
            buffer[:size] = b"\0" * size
            self.left -= size
            return size

    declared_size = addon.MAX_EXTRACTED + 1024
    bomb = io.BytesIO()
    with tarfile.open(fileobj=bomb, mode="w:gz", compresslevel=1) as writer:
        entry = tarfile.TarInfo("skill/big.bin")
        entry.size = declared_size
        writer.addfile(entry, io.BufferedReader(Zeros(declared_size), buffer_size=1 << 20))

    compressed_ratio = declared_size / len(bomb.getvalue())
    answer = install(client, bomb.getvalue())

    assert compressed_ratio > 100
    assert answer.status == 413
    assert "512 mb" in answer.json["error"]


def test_an_upload_that_is_not_a_gzipped_tar_is_refused(client):
    answer = install(client, b"this is not an archive at all")

    assert answer.status == 400
    assert "not a readable .tar.gz" in answer.json["error"]


def test_an_empty_upload_is_refused(client):
    answer = install(client, b"")

    assert answer.status == 400
    assert answer.json["error"] == "the request body is empty"


def test_an_archive_without_a_skill_md_is_refused(client):
    without = io.BytesIO()
    with tarfile.open(fileobj=without, mode="w:gz") as writer:
        body = b"nothing to see"
        entry = tarfile.TarInfo("wrapper/README.md")
        entry.size = len(body)
        writer.addfile(entry, io.BytesIO(body))

    answer = install(client, without.getvalue())

    assert answer.status == 400
    assert "no SKILL.md" in answer.json["error"]


def test_a_failed_install_leaves_no_staging_directory_behind(addon, client):
    install(client, b"not an archive")

    assert not (addon.SKILLS_DIR / ".incoming").exists()


def test_a_skill_can_be_downloaded_and_holds_what_was_uploaded(client):
    install(client, tar_skill(name="downloadable", extra_files={"references/notes.md": "kept"}))

    answer = client.get("/skills/downloadable/archive")

    assert answer.status == 200
    assert answer.body[:2] == b"\x1f\x8b"
    assert 'filename="downloadable.tar.gz"' in answer.headers["Content-Disposition"]
    with tarfile.open(fileobj=io.BytesIO(answer.body), mode="r:gz") as reader:
        assert "downloadable/references/notes.md" in reader.getnames()


def test_downloading_a_skill_that_is_not_installed_is_a_404(client):
    answer = client.get("/skills/never-installed/archive")

    assert answer.status == 404
    assert "no such skill" in answer.json["error"]


def test_one_skill_can_be_read_on_its_own(client):
    install(client, tar_skill(name="readable", description="what it does"))

    answer = client.get("/skills/readable")

    assert answer.status == 200
    assert answer.json["description"] == "what it does"
    assert answer.json["has_skill_md"] is True
    assert answer.json["files"] == 1
    assert answer.json["bytes"] > 0
    assert answer.json["updated_at"]


def test_reading_a_skill_that_is_not_installed_is_a_404(client):
    answer = client.get("/skills/never-installed")

    assert answer.status == 404


def test_a_skill_can_be_deleted_and_deleting_it_again_is_a_404(client):
    install(client, tar_skill(name="temporary"))

    first = client.request("DELETE", "/skills/temporary")
    second = client.request("DELETE", "/skills/temporary")

    assert first.status == 200
    assert first.json == {"deleted": "temporary"}
    assert second.status == 404


def test_the_skill_count_in_health_follows_what_is_installed(client):
    before = client.get("/health").json["skills"]

    install(client, tar_skill(name="counted"))

    assert client.get("/health").json["skills"] == before + 1


def test_a_skill_directory_starting_with_a_dot_is_not_listed(addon, client):
    (addon.SKILLS_DIR / ".hidden-work-in-progress").mkdir(exist_ok=True)

    listed = [skill["name"] for skill in client.get("/skills").json["skills"]]

    assert ".hidden-work-in-progress" not in listed


def test_a_skill_whose_frontmatter_is_unterminated_still_lists(addon, client):
    broken = addon.SKILLS_DIR / "unterminated"
    broken.mkdir(exist_ok=True)
    (broken / "SKILL.md").write_text("---\nname: unterminated\ndescription: never closed\n")

    answer = client.get("/skills/unterminated")

    assert answer.status == 200
    assert answer.json["description"] is None
