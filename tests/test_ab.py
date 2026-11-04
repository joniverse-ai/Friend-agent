"""router 프롬프트 A/B 테스트의 그룹 나누기와 라벨 고르기. Langfuse 는 부르지 않는다."""
from types import SimpleNamespace

from app import router


def test_off_by_default(monkeypatch):
    monkeypatch.delenv("AB_TEST", raising=False)
    assert router.ab_variant("user-1") is None


def test_same_user_same_group(monkeypatch):
    monkeypatch.setenv("AB_TEST", "on")
    assert len({router.ab_variant("user-1") for _ in range(20)}) == 1
    assert router.ab_variant(None) is None


def test_groups_split_about_half(monkeypatch):
    monkeypatch.setenv("AB_TEST", "on")
    groups = [router.ab_variant(f"user-{i}") for i in range(1000)]
    assert 400 < groups.count("a") < 600


def test_split_changes_share_of_b(monkeypatch):
    monkeypatch.setenv("AB_TEST", "on")
    monkeypatch.setenv("AB_SPLIT", "90")
    groups = [router.ab_variant(f"user-{i}") for i in range(1000)]
    assert 50 < groups.count("b") < 150


def fake_prompt(label, missing=()):
    return SimpleNamespace(label=label, is_fallback=label in missing, version=1)


def test_variant_label_and_production_fallback(monkeypatch):
    asked = []

    def get_prompt(name, label, fallback):
        asked.append(label)
        return fake_prompt(label, missing=("prod-b",))

    monkeypatch.setattr(router.tracing, "get_prompt", get_prompt)
    assert router.load_prompt(variant="a").label == "prod-a"
    asked.clear()
    assert router.load_prompt(variant="b").label == "production"   # prod-b 가 없으면 production
    assert asked == ["prod-b", "production"]


def test_explicit_label_wins_over_variant(monkeypatch):
    monkeypatch.setattr(router.tracing, "get_prompt", lambda name, label, fallback: fake_prompt(label))
    assert router.load_prompt(label="staging", variant="a").label == "staging"
