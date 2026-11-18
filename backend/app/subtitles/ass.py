"""SubtitleService : sous-titres dynamiques au format ASS (rendus par libass dans FFmpeg).

- groupes courts (2-3 mots) lisibles sur mobile ;
- apparition progressive mot par mot, mot prononcé surligné ;
- mots importants (réactions, noms de jeux, nombres) agrandis et colorés ;
- style « émotionnel » (couleur + légère rotation) sur les passages intenses ;
- zone sûre : marges latérales et position verticale dépendant du cadrage ;
- presets dans config/subtitle_presets.yaml (aucun code à modifier).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.core.config import Settings, get_settings
from app.scoring.lexicon import EMOTION_CATEGORIES, compile_keywords, find_hits, normalize
from app.transcription.base import Word

PLAY_W, PLAY_H = 1080, 1920
SAFE_MARGIN_X = 90           # marge latérale (boutons TikTok à droite inclus)
SAFE_TOP, SAFE_BOTTOM = 0.06, 0.82   # au-delà de 82 % : zone de la description / boutons
CHAR_W = 0.58                # largeur moyenne d'un caractère en fraction de la taille de police

EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF☀-➿️‍]+")

DEFAULT_PRESET = {
    "font": "Inter", "font_size": 80, "bold": True, "uppercase": True, "primary_color": "#FFFFFF",
    "highlight_color": "#3DFF6E", "keyword_color": "#FFD400", "outline_color": "#000000", "outline": 6,
    "shadow": 3, "highlight_scale": 115, "keyword_scale": 125, "reveal": "progressive", "position_y": 0.7,
    "animation": "pop", "emotional_style": True, "emotional_color": "#FF3B3B",
}


def ass_color(hex_rgb: str, alpha: int = 0) -> str:
    h = hex_rgb.lstrip("#")
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f"&H{alpha:02X}{b}{g}{r}".upper()


def ass_time(t: float) -> str:
    t = max(0.0, t)
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = t - h * 3600 - m * 60
    cs = int(round((s - int(s)) * 100))
    if cs == 100:
        s, cs = s + 1, 0
    return f"{h:d}:{m:02d}:{int(s):02d}.{cs:02d}"


def strip_emoji(text: str) -> str:
    return EMOJI_RE.sub("", text).strip()


def escape_ass(text: str) -> str:
    return text.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ")


@dataclass
class SubEvent:
    start: float
    end: float
    text: str
    width_est: float
    words: list[str] = field(default_factory=list)


@dataclass
class SubtitleResult:
    ass: str
    srt: str
    events: list[SubEvent]
    preset: str


class SubtitleService:
    def __init__(self, settings: Settings | None = None):
        self.s = settings or get_settings()

    def preset(self, name: str | None) -> dict:
        presets = self.s.subtitle_presets()
        name = name or self.s.get("subtitles.style", "gaming")
        return {**DEFAULT_PRESET, **presets.get(name, presets.get("gaming", {}))}

    # ------------------------------------------------------------------ mots-clés
    def keyword_flags(self, words: list[Word], extra_keywords: list[str] | None = None) -> tuple[set[int], set[int]]:
        """Indices des mots importants et des mots « émotionnels »."""
        important, emotional = set(), set()
        norm = [normalize(w.text).strip(" .,!?…\"'") for w in words]
        hits = find_hits(words)
        for h in hits:
            phrase_tokens = h.phrase.split()
            for i, w in enumerate(words):
                if w.start == h.time:
                    for k in range(i, min(len(words), i + len(phrase_tokens))):
                        important.add(k)
                        if h.category in EMOTION_CATEGORIES:
                            emotional.add(k)
                    break
        if extra_keywords:
            pats = compile_keywords(extra_keywords)
            for i, t in enumerate(norm):
                if any(p.search(t) for p in pats):
                    important.add(i)
        for i, (w, t) in enumerate(zip(words, norm)):
            if any(ch.isdigit() for ch in t) or "!" in w.text:
                important.add(i)
            if "!" in w.text:
                emotional.add(i)
        return important, emotional

    # ------------------------------------------------------------------ groupes
    @staticmethod
    def group_words(words: list[Word], max_words: int, max_chars: int) -> list[list[int]]:
        groups: list[list[int]] = []
        cur: list[int] = []
        chars = 0
        for i, w in enumerate(words):
            token = w.text.strip()
            gap = w.start - words[i - 1].end if i else 0
            prev_end_punct = i > 0 and words[i - 1].text.strip().endswith((".", "!", "?", ",", "…"))
            if cur and (len(cur) >= max_words or chars + len(token) > max_chars or gap >= 0.45 or prev_end_punct):
                groups.append(cur)
                cur, chars = [], 0
            cur.append(i)
            chars += len(token) + 1
        if cur:
            groups.append(cur)
        return groups

    # ------------------------------------------------------------------ génération
    def build(self, words: list[Word], clip_duration: float, preset_name: str | None = None,
              subtitle_y: float | None = None, hook: dict | None = None,
              extra_keywords: list[str] | None = None) -> SubtitleResult:
        p = self.preset(preset_name)
        name = preset_name or self.s.get("subtitles.style", "gaming")
        max_words = int(p.get("max_words_per_line", self.s.get("subtitles.max_words_per_line", 3)))
        max_chars = int(p.get("max_chars_per_line", self.s.get("subtitles.max_chars_per_line", 18)))
        words = [w for w in words if w.end > 0 and w.start < clip_duration and w.text.strip()]
        words = [Word(max(0.0, w.start), min(clip_duration, w.end), w.text, w.prob) for w in words]

        y_rel = subtitle_y if subtitle_y is not None else float(p["position_y"])
        y_rel = min(max(y_rel, SAFE_TOP + 0.1), SAFE_BOTTOM)
        y_px = int(y_rel * PLAY_H)
        important, emotional = self.keyword_flags(words, extra_keywords)
        groups = self.group_words(words, max_words, max_chars)

        fs = int(p["font_size"])
        safe_w = PLAY_W - 2 * SAFE_MARGIN_X
        events: list[SubEvent] = []
        lines: list[str] = []
        for gi, g in enumerate(groups):
            g_start = words[g[0]].start
            next_start = words[groups[gi + 1][0]].start if gi + 1 < len(groups) else clip_duration
            g_end = min(next_start, words[g[-1]].end + 0.35, clip_duration)
            tokens = [escape_ass(words[i].text.strip()) for i in g]
            if p.get("uppercase"):
                tokens = [t.upper() for t in tokens]
            is_emotional = bool(p.get("emotional_style")) and any(i in emotional for i in g)
            # largeur estimée (en tenant compte des agrandissements) -> réduction si besoin
            est = sum(len(t) * fs * CHAR_W * (int(p["keyword_scale"]) / 100 if i in important else 1)
                      for t, i in zip(tokens, g)) + fs * 0.3 * (len(g) - 1)
            line_fs = fs if est <= safe_w else max(40, int(fs * safe_w / est))
            est_final = est * line_fs / fs
            for j, wi in enumerate(g):
                ev_start = g_start if j == 0 else words[wi].start
                ev_end = words[g[j + 1]].start if j + 1 < len(g) else g_end
                if ev_end - ev_start < 0.02:
                    continue
                visible = range(j + 1) if p.get("reveal") == "progressive" else range(len(g))
                parts = []
                for k in visible:
                    idx = g[k]
                    tags = []
                    if idx in important:
                        tags.append(f"\\c{ass_color(p['keyword_color'])}\\fscx{p['keyword_scale']}\\fscy{p['keyword_scale']}")
                    if k == j:
                        color = p.get("emotional_color") if is_emotional and idx in emotional else p["highlight_color"]
                        scale = max(int(p["highlight_scale"]), int(p["keyword_scale"]) if idx in important else 0)
                        tags.append(f"\\c{ass_color(color)}\\fscx{scale}\\fscy{scale}")
                    parts.append(("{" + "".join(tags) + "}" if tags else "") + tokens[k] + ("{\\r}" if tags else ""))
                anim = ""
                if j == 0 and p.get("animation") == "pop":
                    anim = "\\fscx85\\fscy85\\t(0,90,\\fscx105\\fscy105)\\t(90,160,\\fscx100\\fscy100)"
                elif p.get("animation") == "shake" and is_emotional:
                    anim = f"\\frz{2 if j % 2 == 0 else -2}"
                elif j == 0 and p.get("animation") == "shake":
                    anim = "\\fscx85\\fscy85\\t(0,90,\\fscx100\\fscy100)"
                head = f"{{\\an5\\pos({PLAY_W // 2},{y_px})\\fs{line_fs}{anim}}}"
                text = head + " ".join(parts)
                lines.append(f"Dialogue: 1,{ass_time(ev_start)},{ass_time(ev_end)},Sub,,0,0,0,,{text}")
                events.append(SubEvent(round(ev_start, 3), round(ev_end, 3), " ".join(tokens[: j + 1]),
                                       round(est_final, 1), [tokens[k] for k in visible]))

        hook_lines = self._hook_events(hook, p) if hook and hook.get("text") else []
        ass = self._header(p) + "\n".join(hook_lines + lines) + "\n"
        return SubtitleResult(ass=ass, srt=self.to_srt(words, groups), events=events, preset=name)

    def _hook_events(self, hook: dict, p: dict) -> list[str]:
        text = strip_emoji(escape_ass(hook["text"])) if not self.s.get("hooks.render_emoji", False) else escape_ass(hook["text"])
        text = text.upper()
        dur = float(hook.get("duration", self.s.get("hooks.duration", 2.5)))
        y = float(hook.get("y", 0.12))
        y_px = int(min(max(y, SAFE_TOP), 0.45) * PLAY_H)
        anim = "\\fscx70\\fscy70\\t(0,120,\\fscx108\\fscy108)\\t(120,220,\\fscx100\\fscy100)\\fad(0,200)"
        return [f"Dialogue: 2,{ass_time(0)},{ass_time(dur)},Hook,,0,0,0,,{{\\an8\\pos({PLAY_W // 2},{y_px}){anim}}}{text}"]

    @staticmethod
    def _header(p: dict) -> str:
        bold = -1 if p.get("bold") else 0
        return (
            "[Script Info]\nScriptType: v4.00+\nWrapStyle: 0\nScaledBorderAndShadow: yes\n"
            f"PlayResX: {PLAY_W}\nPlayResY: {PLAY_H}\n\n"
            "[V4+ Styles]\n"
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
            "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, "
            "MarginR, MarginV, Encoding\n"
            f"Style: Sub,{p['font']},{p['font_size']},{ass_color(p['primary_color'])},{ass_color(p['highlight_color'])},"
            f"{ass_color(p['outline_color'])},{ass_color('#000000', 0x80)},{bold},0,0,0,100,100,1,0,1,{p['outline']},"
            f"{p['shadow']},5,{SAFE_MARGIN_X},{SAFE_MARGIN_X},0,1\n"
            f"Style: Hook,{p['font']},70,{ass_color('#111111')},{ass_color('#111111')},{ass_color('#FFFFFF')},"
            f"{ass_color('#FFFFFF')},-1,0,0,0,100,100,0,0,3,18,0,8,{SAFE_MARGIN_X},{SAFE_MARGIN_X},0,1\n\n"
            "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        )

    @staticmethod
    def to_srt(words: list[Word], groups: list[list[int]]) -> str:
        def ts(t: float) -> str:
            ms = int(round(max(0.0, t) * 1000))
            h, ms = divmod(ms, 3600000)
            m, ms = divmod(ms, 60000)
            s, ms = divmod(ms, 1000)
            return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

        out = []
        for n, g in enumerate(groups, 1):
            out.append(f"{n}\n{ts(words[g[0]].start)} --> {ts(words[g[-1]].end)}\n"
                       + " ".join(words[i].text.strip() for i in g) + "\n")
        return "\n".join(out)
