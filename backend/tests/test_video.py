"""Analyse vidéo, crop 9:16 (toutes les stratégies), rendu et contrôle qualité."""
import subprocess

import pytest

from app.clips.composition import LAYOUTS, VideoCompositionService, piecewise_expr
from app.clips.tracking import NoFaceDetector
from app.services.quality import QualityControlService
from app.services.render import RenderService
from app.subtitles.ass import SubtitleService
from app.video import ffmpeg
from app.video.analysis import VideoAnalysisService

from .conftest import FakeFaceDetector, make_words, requires_ffmpeg

pytestmark = requires_ffmpeg


def test_probe_and_analysis(synthetic_video, tmp_path, settings):
    info = ffmpeg.probe(synthetic_video)
    assert (info.width, info.height) == (1280, 720) and info.has_audio and abs(info.duration - 40) < 0.2
    res = VideoAnalysisService(settings).analyze(synthetic_video, tmp_path)
    assert len(res.audio_db) == 80 and len(res.motion) == 80
    loud = max(res.audio_db[40:46])
    calm = sum(res.audio_db[10:30]) / 20
    assert loud - calm > 15, "l'explosion sonore à 20-23 s doit ressortir"
    assert len(res.dhash) >= 39
    assert (tmp_path / "analysis.json").exists()
    # cache : le second appel relit le fichier
    assert VideoAnalysisService(settings).analyze(synthetic_video, tmp_path).audio_db == res.audio_db


def test_piecewise_expression_is_valid_for_ffmpeg():
    expr = piecewise_expr([(0, 100), (1, 200), (2, 150)])
    assert expr.startswith("if(lt(t,1)")
    assert piecewise_expr([(0, 100), (1, 100.4)]) == "100"


def test_auto_layout_detects_webcam_and_splits(synthetic_video, settings):
    comp = VideoCompositionService(settings, face_detector=FakeFaceDetector())
    plan = comp.plan(synthetic_video, 1280, 720, 5, 20)
    assert plan.layout == "split"
    assert plan.details["face_inside_ratio"] == 1.0
    big = VideoCompositionService(settings, face_detector=FakeFaceDetector((0.35, 0.2, 0.3, 0.45)))
    assert big.plan(synthetic_video, 1280, 720, 5, 20).layout == "tracking"
    none = VideoCompositionService(settings, face_detector=NoFaceDetector())
    assert none.plan(synthetic_video, 1280, 720, 5, 20).layout == "gameplay"


@pytest.mark.parametrize("layout", LAYOUTS)
def test_every_layout_renders_1080x1920(layout, synthetic_video, tmp_path, settings):
    comp = VideoCompositionService(settings, face_detector=FakeFaceDetector())
    plan = comp.plan(synthetic_video, 1280, 720, 18, 21, analysis=VideoAnalysisService(settings).analyze(synthetic_video, tmp_path),
                     layout=layout, crop_override={"webcam_region": [0.02, 0.6, 0.25, 0.35]})
    out = tmp_path / f"{layout}.mp4"
    RenderService(settings).render(synthetic_video, 18, 21, plan, out, None, True, 30)
    info = ffmpeg.probe(out)
    assert (info.width, info.height) == (1080, 1920)
    assert info.video_codec == "h264" and info.audio_codec == "aac"
    assert abs(info.duration - 3) < 0.25


def _render_with_subs(src, out, settings, start=15.0, end=31.0, layout="gameplay"):
    comp = VideoCompositionService(settings, face_detector=NoFaceDetector())
    plan = comp.plan(src, 1280, 720, start, end, layout=layout)
    words = make_words([(1, "Attends ! Non non non !"), (5, "C'est pas possible !")])
    subs = SubtitleService(settings).build(words, end - start, "gaming", subtitle_y=plan.subtitle_y,
                                           hook={"text": "ATTENDS DE VOIR…", "duration": 2})
    ass = out.with_suffix(".ass")
    ass.write_text(subs.ass)
    RenderService(settings).render(src, start, end, plan, out, ass, True, 30)
    return plan, subs


def test_quality_control_passes_on_good_clip(synthetic_video, tmp_path, settings):
    out = tmp_path / "good.mp4"
    plan, subs = _render_with_subs(synthetic_video, out, settings)
    report = QualityControlService(settings).check(out, subs.events, plan.to_dict(), hook_text="ATTENDS DE VOIR…")
    assert report.passed, report.reasons
    names = {c.name for c in report.checks}
    assert {"file_valid", "duration", "resolution", "codecs", "audio", "black_frames", "subtitles_sync", "text_fits"} <= names


def test_quality_control_flags_bad_clips(tmp_path, settings):
    bad = tmp_path / "bad.mp4"
    # 640x360, 5 s, image noire, sans audio : plusieurs défauts
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=black:s=640x360:d=5", "-c:v", "libx264",
                    "-pix_fmt", "yuv420p", str(bad)], check=True)
    report = QualityControlService(settings).check(bad, [], {"face_inside_ratio": 0.5}, expect_subtitles=True)
    assert not report.passed
    failed = {c.name for c in report.checks if not c.ok}
    assert {"duration", "resolution", "audio", "black_frames", "face_framing"} <= failed
    assert any("visage coupé" in r for r in report.reasons)


def test_quality_control_rejects_corrupt_file(tmp_path, settings):
    junk = tmp_path / "junk.mp4"
    junk.write_bytes(b"not a video" * 100)
    report = QualityControlService(settings).check(junk)
    assert not report.passed and "illisible" in report.reasons[0]


def test_render_command_avoids_removed_ffmpeg_options(tmp_path, settings, monkeypatch):
    """-filter_complex_script a été supprimée dans FFmpeg 9 : le graphe doit passer par -filter_complex."""
    from app.clips.composition import CompositionPlan
    from app.services import render as render_mod

    seen = []
    monkeypatch.setattr(render_mod.ffmpeg, "run", lambda cmd, timeout=None: seen.append(cmd))
    plan = CompositionPlan("gameplay", "[0:v]scale=1080:1920[vcomp]", subtitle_y=0.7)
    RenderService(settings).render(tmp_path / "in.mp4", 0, 5, plan, tmp_path / "out.mp4", None, True, 30)
    cmd = seen[0]
    assert "-filter_complex_script" not in cmd and "-filter_script" not in cmd and "-vsync" not in cmd
    assert cmd[cmd.index("-filter_complex") + 1].startswith("[0:v]scale=1080:1920[vcomp]")
    # graphe géant : syntaxe moderne « -/filter_complex fichier »
    monkeypatch.setattr(render_mod, "MAX_INLINE_GRAPH", 10)
    RenderService(settings).render(tmp_path / "in.mp4", 0, 5, plan, tmp_path / "out2.mp4", None, True, 30)
    assert "-/filter_complex" in seen[1] and "-filter_complex_script" not in seen[1]


def _gray_frame(path, t):
    out = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(t), "-i", str(path), "-frames:v", "1", "-f", "rawvideo",
                          "-pix_fmt", "gray", "-"], capture_output=True, check=True).stdout
    import numpy as np
    return np.frombuffer(out, np.uint8).reshape(1920, 1080).astype(int)


def test_punch_effects_and_progress_bar(synthetic_video, tmp_path, settings, monkeypatch):
    """Zoom + flash au moment fort, barre de progression qui avance ; image intacte hors du pic."""
    comp = VideoCompositionService(settings, face_detector=NoFaceDetector())
    plan = comp.plan(synthetic_video, 1280, 720, 10, 16, layout="gameplay")
    plain, fx = tmp_path / "plain.mp4", tmp_path / "fx.mp4"
    monkeypatch.setitem(settings, "effects", {"enabled": False})
    RenderService(settings).render(synthetic_video, 10, 16, plan, plain, None, True, 30)
    monkeypatch.setitem(settings, "effects", {"enabled": True, "punch_zoom": 1.2, "punch_duration": 1.0, "shake": True,
                                              "flash": True, "progress_bar": True, "bar_height": 12})
    RenderService(settings).render(synthetic_video, 10, 16, plan, fx, None, True, 30, peak=3.0)
    assert (ffmpeg.probe(fx).width, ffmpeg.probe(fx).height) == (1080, 1920)
    a, b = _gray_frame(plain, 1.5), _gray_frame(fx, 1.5)
    assert abs(a[100:] - b[100:]).mean() < 3, "hors du pic, l'image ne doit pas changer"
    a, b = _gray_frame(plain, 3.5), _gray_frame(fx, 3.5)
    assert abs(a[100:] - b[100:]).mean() > 5, "au pic, le zoom doit modifier l'image"
    bar_early = (_gray_frame(fx, 1.0)[4] > 150).sum()
    bar_late = (_gray_frame(fx, 5.0)[4] > 150).sum()
    assert bar_late > bar_early + 300, (bar_early, bar_late)


def test_effects_disabled_produce_no_filters():
    from app.services.render import effects_chain

    assert effects_chain(2.0, 10, {"enabled": False}) == ("", "")
    pre, post = effects_chain(None, 10, {"enabled": True})
    assert pre == "" and "overlay" in post
    pre, _ = effects_chain(9.9, 10, {"enabled": True})
    assert pre == "", "pic trop proche de la fin : pas d'effet coupé net"
