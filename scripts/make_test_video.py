#!/usr/bin/env python3
"""Génère une vidéo de test 100 % libre de droits : faux stream « GTA 6 » commenté en français.

- voix : ElevenLabs (si l'API est accessible), sinon bips synthétiques (pas de transcription utile) ;
- image : gameplay dessiné (route, voiture, HUD, minimap, cadre webcam), explosions et
  changements de plan pendant les moments forts.

Usage : python scripts/make_test_video.py [--out data/test/test_stream.mp4]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
import wave
from pathlib import Path

import numpy as np

SR = 48000
W, H, FPS = 1280, 720, 30
VOICE = os.environ.get("TEST_VOICE_ID", "TX3LPaxmHKxFdv7VOQHJ")  # « Liam », voix énergique

SCRIPT = [
    ("calm", "Salut tout le monde, bienvenue sur le stream ! Aujourd'hui on continue GTA 6, on est à Vice City et on va tenter le braquage de la banque."),
    ("calm", "Bon, je prends la voiture, et pour une fois je respecte le code de la route. Tranquille."),
    ("calm", "Le chat me demande si les graphismes sont aussi beaux en vrai. Franchement oui, regardez l'eau, regardez les reflets."),
    ("pause", 2.5),
    ("hype", "Attends, attends... C'est quoi ça ? Non non non ! C'est pas possible ! Il m'a percuté de nulle part !"),
    ("calm", "Bon. On respire. La voiture est complètement détruite, je vais en prendre une autre."),
    ("calm", "Je passe par le pont, normalement il n'y a personne à cette heure-ci."),
    ("calm", "Petite parenthèse, merci beaucoup pour les abonnements, ça fait super plaisir. On est presque à l'objectif du jour."),
    ("pause", 1.5),
    ("hype", "Ha ha ha ha ! Mais il est tombé dans l'eau avec son jet ski ! Je suis mort de rire, regardez-le !"),
    ("calm", "Ok, on arrive à la banque. Il faut être discret. On attend que le garde s'éloigne."),
    ("pause", 4.0),
    ("calm", "Doucement... doucement... on y est presque."),
    ("pause", 1.0),
    ("hype", "Let's go ! On l'a fait ! Le braquage est réussi, incroyable, c'est le clutch de l'année !"),
    ("calm", "Bon, maintenant il faut semer la police. Ça va être compliqué."),
    ("calm", "Je tourne à gauche, puis à droite, j'essaie de rester dans les petites rues pour les perdre."),
    ("calm", "Encore un peu, la planque est juste derrière le stade, on y est presque."),
    ("hype", "Oh non... Oh non non non ! Je suis mort ! Game over ! J'y crois pas, juste avant la planque !"),
    ("calm", "Bon. On recommence depuis la dernière sauvegarde. Merci d'être là, à tout de suite pour la suite du stream."),
]


def tts(text: str, hype: bool, cache: Path) -> np.ndarray | None:
    import hashlib

    key = hashlib.sha1(f"{VOICE}{hype}{text}".encode()).hexdigest()[:16]
    mp3 = cache / f"{key}.mp3"
    if not mp3.exists():
        body = {
            "text": text, "model_id": "eleven_multilingual_v2",
            "voice_settings": {"stability": 0.25 if hype else 0.6, "similarity_boost": 0.8,
                               "style": 0.85 if hype else 0.2, "use_speaker_boost": True},
        }
        res = subprocess.run(
            ["curl", "-sS", "-f", "-X", "POST", f"https://api.elevenlabs.io/v1/text-to-speech/{VOICE}",
             "-H", f"xi-api-key: {os.environ.get('ELEVENLABS_API_KEY', 'proxy-injected')}",
             "-H", "Content-Type: application/json", "-d", json.dumps(body), "-o", str(mp3)],
            capture_output=True, text=True,
        )
        if res.returncode != 0:
            mp3.unlink(missing_ok=True)
            print("ElevenLabs indisponible :", res.stderr.strip()[:200])
            return None
    wav = mp3.with_suffix(".wav")
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(mp3), "-ac", "1", "-ar", str(SR), str(wav)], check=True)
    with wave.open(str(wav)) as w:
        data = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768
    return data * (1.25 if hype else 0.8)


def beep_voice(seconds: float, hype: bool) -> np.ndarray:
    t = np.arange(int(seconds * SR)) / SR
    env = (np.sin(2 * np.pi * 3 * t) > 0).astype(np.float32)
    return (0.5 if hype else 0.15) * env * np.sin(2 * np.pi * (300 if hype else 180) * t)


def build_audio(cache: Path) -> tuple[np.ndarray, list[dict]]:
    pieces, timeline, t = [], [], 0.5
    pieces.append(np.zeros(int(0.5 * SR), np.float32))
    for kind, value in SCRIPT:
        if kind == "pause":
            pieces.append(np.zeros(int(value * SR), np.float32))
            timeline.append({"kind": "pause", "start": t, "end": t + value})
            t += value
            continue
        hype = kind == "hype"
        audio = tts(value, hype, cache)
        if audio is None:
            audio = beep_voice(max(2.0, len(value) / 14), hype)
        gap = np.zeros(int(0.45 * SR), np.float32)
        pieces += [audio, gap]
        dur = len(audio) / SR
        timeline.append({"kind": kind, "start": t, "end": t + dur, "text": value})
        t += dur + 0.45
    pieces.append(np.zeros(int(1.5 * SR), np.float32))
    voice = np.concatenate(pieces)
    # ambiance jeu : moteur + bruit, explosions pendant les moments forts
    n = voice.size
    tt = np.arange(n) / SR
    rng = np.random.default_rng(7)
    game = 0.02 * np.sin(2 * np.pi * 55 * tt) + 0.01 * rng.standard_normal(n)
    for seg in timeline:
        if seg["kind"] == "hype":
            i0 = int(seg["start"] * SR)
            boom = np.exp(-np.arange(int(1.2 * SR)) / (0.25 * SR)) * rng.standard_normal(int(1.2 * SR)) * 0.35
            game[i0:i0 + boom.size] += boom[: max(0, min(boom.size, n - i0))]
    mix = np.clip(voice + game, -1, 1)
    return mix, timeline


def draw_frames(duration: float, timeline: list[dict]):
    import cv2

    rng = np.random.default_rng(3)
    hype = [(s["start"], s["end"]) for s in timeline if s["kind"] == "hype"]
    palettes = [((40, 30, 20), (60, 120, 200)), ((70, 40, 90), (40, 160, 255)), ((20, 60, 30), (80, 200, 120))]
    pal = 0
    car_x = W * 0.5
    particles: list[list[float]] = []
    for f in range(int(duration * FPS)):
        t = f / FPS
        in_hype = any(a - 0.2 <= t <= b for a, b in hype)
        if in_hype and f % 18 == 0:
            pal = (pal + 1) % len(palettes)       # changements de plan rapides
        sky, ground = palettes[pal]
        img = np.zeros((H, W, 3), np.uint8)
        img[: H // 2] = sky
        img[H // 2:] = ground
        speed = 40 if in_hype else 8
        # route en perspective
        cv2.fillPoly(img, [np.array([[W * 0.42, H // 2], [W * 0.58, H // 2], [W * 0.95, H], [W * 0.05, H]], np.int32)], (60, 60, 60))
        for k in range(8):
            y = H // 2 + ((k * 60 + f * speed) % (H // 2))
            cv2.rectangle(img, (W // 2 - 4, int(y)), (W // 2 + 4, int(y + 25)), (230, 230, 230), -1)
        # immeubles
        for k in range(10):
            x = int((k * 160 - f * speed * 0.6) % (W + 160)) - 80
            hgt = 80 + (k * 37) % 120
            cv2.rectangle(img, (x, H // 2 - hgt), (x + 90, H // 2), (90 + k * 8, 80, 70 + k * 5), -1)
        # voiture du joueur (bouge davantage pendant l'action)
        car_x += (rng.standard_normal() * (25 if in_hype else 2))
        car_x = float(np.clip(car_x, W * 0.3, W * 0.7))
        cv2.rectangle(img, (int(car_x) - 50, H - 170), (int(car_x) + 50, H - 100), (30, 30, 200), -1)
        # explosions
        if in_hype and rng.random() < 0.5:
            cx, cy = rng.integers(200, W - 200), rng.integers(150, H - 250)
            for _ in range(25):
                particles.append([cx, cy, rng.normal(0, 12), rng.normal(-6, 10), 1.0])
        for p in particles:
            p[0] += p[2]; p[1] += p[3]; p[3] += 0.8; p[4] -= 0.04
            if p[4] > 0:
                cv2.circle(img, (int(p[0]), int(p[1])), int(6 + 10 * p[4]), (0, int(140 + 100 * p[4]), 255), -1)
        particles[:] = [p for p in particles if p[4] > 0]
        if in_hype and f % 9 < 2:
            img = cv2.addWeighted(img, 0.5, np.full_like(img, 255), 0.5, 0)  # flash
        # HUD
        cv2.rectangle(img, (30, 30), (330, 52), (0, 0, 0), -1)
        cv2.rectangle(img, (32, 32), (32 + int(296 * (0.4 if in_hype else 0.9)), 50), (40, 200, 40), -1)
        cv2.putText(img, "$ 1,250,000", (30, 90), cv2.FONT_HERSHEY_DUPLEX, 1.0, (120, 255, 120), 2)
        cv2.circle(img, (W - 110, H - 110), 80, (30, 30, 30), -1)
        cv2.circle(img, (W - 110, H - 110), 80, (200, 200, 200), 2)
        # cadre « webcam » du streamer
        cv2.rectangle(img, (20, H - 250), (330, H - 20), (25, 25, 25), -1)
        face_x = 175 + int(8 * np.sin(t * 2))
        cv2.circle(img, (face_x, H - 150), 60, (150, 180, 220), -1)
        mouth = 22 if in_hype else 6
        cv2.ellipse(img, (face_x, H - 125), (25, mouth), 0, 0, 360, (40, 40, 120), -1)
        cv2.circle(img, (face_x - 22, H - 165), 7, (30, 30, 30), -1)
        cv2.circle(img, (face_x + 22, H - 165), 7, (30, 30, 30), -1)
        cv2.putText(img, "LIVE", (30, H - 225), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (60, 60, 255), 2)
        yield img


def main() -> None:
    ap = argparse.ArgumentParser()
    root = Path(__file__).resolve().parents[1]
    ap.add_argument("--out", default=str(root / "data" / "test" / "test_stream_gta6.mp4"))
    args = ap.parse_args()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    cache = out.parent / "tts_cache"
    cache.mkdir(exist_ok=True)

    audio, timeline = build_audio(cache)
    duration = audio.size / SR
    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / "mix.wav"
        with wave.open(str(wav), "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR)
            w.writeframes((audio * 32767).astype(np.int16).tobytes())
        proc = subprocess.Popen(
            ["ffmpeg", "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{W}x{H}", "-r", str(FPS),
             "-i", "-", "-i", str(wav), "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-b:a", "128k", "-shortest", str(out)],
            stdin=subprocess.PIPE,
        )
        assert proc.stdin
        for frame in draw_frames(duration, timeline):
            proc.stdin.write(frame.tobytes())
        proc.stdin.close()
        proc.wait()
    (out.with_suffix(".timeline.json")).write_text(json.dumps(timeline, ensure_ascii=False, indent=1))
    print(f"OK : {out} ({duration:.1f}s) — moments forts attendus :")
    for s in timeline:
        if s["kind"] == "hype":
            print(f"  {s['start']:6.1f}s → {s['end']:6.1f}s  {s['text'][:60]}")


if __name__ == "__main__":
    main()
