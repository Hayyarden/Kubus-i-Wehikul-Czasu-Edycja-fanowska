#!/usr/bin/env python3
"""Patcher poziomów pełnej wersji "Kubuś i Wehikuł Czasu" (pliki .dcr, Shockwave/Director 8.5).

Poziomy z kubus.pl to skompresowane filmy Shockwave ("Afterburner"). Patcher:
  1. Ustawia stałe tempo (domyślnie 30 kl./s). Gra przesuwa postać o stałą wartość
     na klatkę, a filmy mają tempo 150-200 kl./s. Tempo jest pamiętane w zmiennej
     globalnej, więc ustawienie zmienione klawiszami +/- przechodzi na kolejne poziomy.
  2. Dodaje regulację tempa klawiszami +/- (tak jak w poprawionym Kuba.exe).
  3. Przywraca przechodzenie między poziomami. Przyciski na ekranach końca poziomu
     wołały gotoNetPage("zapisz_graj.php?nr=N") na nieistniejącej stronie kubus.pl.
     Nazwa gotoNetPage jest podmieniana na handler kfGoto: "Gram dalej" ładuje
     następny plik .dcr, a "Zakończ"/"Tak" na ekranie Esc zamykają grę.

Użycie:
  python kubus_dcr_patch.py KATALOG_Z_DCR KATALOG_WYJŚCIOWY [--fps 30]

Pliki muszą nazywać się jak w oryginale (01_droga_do_zamku.dcr ... 11_pokoj_badan.dcr),
bo kolejność poziomów wynika z numeru na początku nazwy.
"""
import argparse
import glob
import os
import struct
import sys
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kubus_patch as kp  # noqa: E402  (wspólny asembler/budowanie skryptów Lingo)


def read_varint(d, p):
    v = 0
    while True:
        b = d[p]
        p += 1
        v = (v << 7) | (b & 0x7f)
        if not b & 0x80:
            return v, p


def varint(v):
    out = [v & 0x7f]
    v >>= 7
    while v:
        out.append(0x80 | (v & 0x7f))
        v >>= 7
    return bytes(reversed(out))


ILS_RESIDENT = 0xFFFFFFFF


class Afterburner:
    """Odczyt i zapis filmu Shockwave (XFIR/FGDM) z podmianą zasobów w ILS."""

    def __init__(self, data):
        d = data
        assert d[:4] == b"XFIR" and d[8:12] == b"MDGF", "to nie jest plik .dcr (Afterburner)"
        p = 12
        assert d[p:p + 4] == b"revF"
        ln, q = read_varint(d, p + 4)
        p = q + ln
        assert d[p:p + 4] == b"rdcF"
        ln, q = read_varint(d, p + 4)
        p = q + ln
        self.head = d[12:p]                      # Fver + Fcdr bez zmian
        assert d[p:p + 4] == b"PMBA"
        ln, q = read_varint(d, p + 4)
        abmp_end = q + ln
        self.abmp_ct, q = read_varint(d, q)
        _ulen, q = read_varint(d, q)
        ab = zlib.decompress(d[q:abmp_end])
        p = abmp_end
        q = 0
        self.u1, q = read_varint(ab, q)
        self.u2, q = read_varint(ab, q)
        n, q = read_varint(ab, q)
        self.entries = []                        # [rid, off, cs, us, ct, tag(4 B)]
        for _ in range(n):
            e = []
            for _ in range(5):
                v, q = read_varint(ab, q)
                e.append(v)
            e.append(ab[q:q + 4])
            q += 4
            self.entries.append(e)
        self.by_id = {e[0]: e for e in self.entries}
        assert d[p:p + 4] == b"IEGF"
        self.fgei_unk, p = read_varint(d, p + 4)
        self.body = d[p:]
        ils = [e for e in self.entries if e[5][::-1] == b"ILS "]
        assert len(ils) == 1 and ils[0][1] == 0, "nieoczekiwany układ ILS"
        self.ils_entry = ils[0]
        raw = zlib.decompress(self.body[:self.ils_entry[2]])
        self.ils = []                            # [(rid, data)] w oryginalnej kolejności
        q = 0
        while q < len(raw):
            rid, q = read_varint(raw, q)
            size = self.by_id[rid][3]
            self.ils.append([rid, raw[q:q + size]])
            q += size
        self.ils_index = {rid: i for i, (rid, _) in enumerate(self.ils)}

    def tag(self, rid):
        return self.by_id[rid][5][::-1].decode("latin1")

    def ids(self, tag):
        return [e[0] for e in self.entries if self.tag(e[0]) == tag]

    def get(self, rid):
        if rid in self.ils_index:
            return self.ils[self.ils_index[rid]][1]
        e = self.by_id[rid]
        raw = self.body[e[1]:e[1] + e[2]]
        return zlib.decompress(raw) if e[4] == 0 else raw

    def set(self, rid, data):
        assert rid in self.ils_index, "podmiana obsługiwana tylko dla zasobów z ILS"
        self.ils[self.ils_index[rid]][1] = bytes(data)
        e = self.by_id[rid]
        e[2] = e[3] = len(data)

    def serialize(self):
        raw = b"".join(varint(rid) + data for rid, data in self.ils)
        comp = zlib.compress(raw, 9)
        old_cs = self.ils_entry[2]
        delta = len(comp) - old_cs
        for e in self.entries:
            if e is self.ils_entry:
                continue
            if e[1] != ILS_RESIDENT:
                assert e[1] >= old_cs
                e[1] += delta
        self.ils_entry[2] = len(comp)
        self.ils_entry[3] = len(raw)
        body = comp + self.body[old_cs:]
        self.body = body

        ab = varint(self.u1) + varint(self.u2) + varint(len(self.entries))
        for rid, off, cs, us, ct, tag in self.entries:
            ab += varint(rid) + varint(off) + varint(cs) + varint(us) + varint(ct) + tag
        abc = zlib.compress(ab, 9)
        abmp_payload = varint(self.abmp_ct) + varint(len(ab)) + abc
        out = (self.head + b"PMBA" + varint(len(abmp_payload)) + abmp_payload
               + b"IEGF" + varint(self.fgei_unk) + body)
        return b"XFIR" + struct.pack("<I", len(out) + 4) + b"MDGF" + out


def lingo_context(movie):
    best = None
    for rid in movie.ids("LctX"):
        blob = movie.get(rid)
        count = struct.unpack_from(">I", blob, 8)[0]
        entries_off = struct.unpack_from(">H", blob, 16)[0]
        lnam = struct.unpack_from(">i", blob, 32)[0]
        scripts = [struct.unpack_from(">i", blob, entries_off + 12 * k + 4)[0]
                   for k in range(count)]
        scripts = [s for s in scripts if s >= 0 and s in movie.by_id]
        if best is None or len(scripts) > len(best[1]):
            best = (lnam, scripts)
    return best


def prepare_movie_program(fps):
    p = kp.prepare_movie_program(0, "none")[:-1]   # oryginalna treść
    p += [
        # if voidP(kfTempo) then kfTempo = fps   (tempo przechodzi między poziomami)
        ("getglobal", "kfTempo"), ("pusharglist", 1), ("extcall", "voidP"),
        ("jmpifz", "have_tempo"),
        ("pushint", fps), ("setglobal", "kfTempo"),
        ("label", "have_tempo"),
        ("pushzero",), ("setglobal", "kfUpWas"), ("pushzero",), ("setglobal", "kfDownWas"),
        ("getglobal", "kfTempo"), ("pusharglistnoret", 1), ("extcall", "puppetTempo"),
        ("ret",),
    ]
    return p


def goto_program(next_movie):
    """on kfGoto url - zastępuje gotoNetPage z przycisków gry."""
    p = []
    if next_movie:
        p += [
            ("getparam", 0), ("pushcons", "graj"), ("containsstr",), ("jmpifz", "quit"),
            ("pushint", 1), ("pushcons", next_movie), ("pusharglistnoret", 2), ("extcall", "go"),
            ("ret",),
            ("label", "quit"),
        ]
    return p + [("pusharglistnoret", 0), ("extcall", "quit"), ("ret",)]


def patch_level(data, next_movie, fps):
    movie = Afterburner(data)
    lnam, scripts = lingo_context(movie)
    names = kp.Names(movie.get(lnam))
    if "gotoNetPage" not in names.names:
        raise SystemExit("brak wywołań gotoNetPage - nieznana wersja poziomu")
    names.names[names.names.index("gotoNetPage")] = "kfGoto"

    target = [s for s in scripts
              if kp.handler_names(movie.get(s), names.names) == ["preparemovie"]]
    original, _ = kp.assemble(kp.prepare_movie_program(0, "none"), names, [])
    if len(target) != 1 or kp.single_handler_code(movie.get(target[0])) != original:
        raise SystemExit("nieoczekiwany skrypt prepareMovie")
    loop_code = (kp.LOOP_FRAME_CODE
                 .replace(b"{frame}", bytes([names.id("frame")]))
                 .replace(b"{go}", bytes([names.id("go")])))
    loops = [s for s in scripts
             if kp.handler_names(movie.get(s), names.names) == ["exitframe"]
             and kp.single_handler_code(movie.get(s)) == loop_code]
    if not loops:
        raise SystemExit("nie znaleziono skryptu pętli gry")

    new = {target[0]: kp.build_script(movie.get(target[0]), names, [
        ("prepareMovie", [], prepare_movie_program(fps), None),
        ("kfTempoKeys", [], kp.tempo_keys_program(), None),
        ("kfGoto", ["url"], goto_program(next_movie), None),
    ], extra_globals=("kfTempo", "kfUpWas", "kfDownWas"))}
    for s in loops:
        new[s] = kp.build_script(movie.get(s), names, [
            ("exitFrame", ["me"], kp.loop_frame_program(), kp.EVENT_EXIT_FRAME)])
    movie.set(lnam, names.serialize())
    for rid, blob in new.items():
        movie.set(rid, blob)
    return movie.serialize()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("src", help="katalog z oryginalnymi plikami .dcr")
    ap.add_argument("dst", help="katalog na poprawione pliki")
    ap.add_argument("--fps", type=int, default=30, help="tempo startowe (10-80, domyślnie 30)")
    args = ap.parse_args(argv)
    if not kp.TEMPO_MIN <= args.fps <= kp.TEMPO_MAX:
        ap.error(f"--fps musi być w zakresie {kp.TEMPO_MIN}-{kp.TEMPO_MAX}")
    files = sorted(os.path.basename(f) for f in glob.glob(os.path.join(args.src, "[0-9][0-9]_*.dcr")))
    if not files:
        raise SystemExit("nie znaleziono plików NN_*.dcr")
    os.makedirs(args.dst, exist_ok=True)
    for i, name in enumerate(files):
        nxt = files[i + 1] if i + 1 < len(files) else None
        data = open(os.path.join(args.src, name), "rb").read()
        out = patch_level(data, nxt, args.fps)
        with open(os.path.join(args.dst, name), "wb") as f:
            f.write(out)
        print(f"{name}: {len(data)} -> {len(out)} B, dalej: {nxt or 'koniec gry'}")


if __name__ == "__main__":
    sys.exit(main())
