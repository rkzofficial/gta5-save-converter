# Research log: GTA V PC → PS5 save conversion

A summary of how the converter came together over one long session, including the wrong turns. They explain most of what took time.

## Goal and setup

The goal was a GTA V story save at a specific completion percentage on a PS5, starting from a PC save at 38.4%. No PC→PS5 converter for GTA V existed. The one candidate found, [ps5-save-converter](https://github.com/sirthizzalot/ps5-save-converter), only handles Unreal Engine GVAS saves, and GTA V runs on Rockstar's RAGE engine.

Setup used:

- a jailbroken PS5 running Garlic SaveManager, with its web UI and HTTP API, and an FTP server;
- the PS5 game's decrypted `eboot.bin`, copied from the console;
- `GTA5_Enhanced.exe`, fetched on its own from the Epic Games Store with [Legendary](https://github.com/derrod/legendary) (`install <app> --prefix GTA5_Enhanced.exe`), instead of downloading the full 100 GB game;
- Python with Capstone for disassembly.

## 1. First look at both saves

- **PC `SGTA50015`:** a plaintext header holding version 1, a UTF-16 title (`(Autosave) Mr. Richards (38.4%) - 09/20/26 18:05:54`) and a 4-byte field. The rest is encrypted. Repeated 16-byte blocks pointed to AES in ECB mode.
- **PS5:** Garlic's first export was the sealed PFS images (`sdimg_*`), which have PFS magic `20130315` and full-entropy contents. Mounting the save through Garlic gave the real files: `memory.dat` (1 MiB; this is the autosave) plus `sce_sys/param.sfo`. Its layout matched the PC save: the same title header, then encrypted data.

## 2. Finding the AES keys (known-plaintext scan)

Both encrypted bodies repeat one 16-byte block thousands of times, which is almost certainly encrypted zeros. A small C scanner (macOS CommonCrypto, not included here) tries every 16- and 32-byte window of a binary as an AES key and reports the windows that decrypt that block to zeros. It scans 20 MB in about 6 seconds.

- **PS5 key:** found in `eboot.bin` (70 MB) in 19 seconds, at file offset `0x3f23850`.
- **PC key:** found in `GTA5_Enhanced.exe` (56 MB) in 14 seconds, at offset `0x289c400`. That `.exe` is mostly packed, but the key sits in the unpacked part. A Pastebin post lists Xbox 360 and PS3 archive keys; neither decrypts PC saves.

Both keys produce readable data: a `PSIN` tag, `PMAP`, plain-text radio station names.

## 3. Wrong turn: the "16-byte prefix"

I decrypted the PS5 file from offset `0x104`, right after the title, because that's where the PC data starts. That produced 16 bytes of noise followed by `PSIN`. I took those 16 bytes to be an encrypted checksum and spent a long time on them: MD5, SHA-1/256/512, HMAC, AES-CMAC and CBC-MAC over every byte range that made sense. Nothing matched.

**They were never encrypted.** The PS5 header is `0x114` bytes long and the encrypted data starts at `0x114`. Decrypting from `0x104` only appeared to work because ECB encrypts each 16-byte block independently, and `0x104` and `0x114` sit at the same position within a block. The 16 bytes are three plaintext fields: a title hash, the data length and a timestamp.

## 4. Console tests and what they really meant

Every test followed the same routine: mount the main autosave through Garlic's API, upload the file, read it back and compare SHA-256 hashes, unmount, then load the game.

| Test | Change | What the game did | Read at the time | Actual reason |
|---|---|---|---|---|
| first | PC data + PC title | "(Backup)" | rejected | title hash didn't match the title |
| control | 1 filler byte | loaded 0.8% | accepted | CHKS invalid → silent default state |
| A / B | version fields swapped | (Backup) / loaded | B: version fields aren't checked | A: title hash. B: CHKS invalid, silent default |
| C / E | hybrid PS5 + PC data | (Backup) / loaded 0.8% | E: title is the problem | C: title hash. E: CHKS invalid |
| D | 1 data byte | loaded 0.8% | no checksum | CHKS invalid, silent default |
| F / G | full or realigned PC data | loaded, "fresh 0.8%" | PC layout rejected | CHKS / length mismatch, silent default |
| H | progress block zeroed | (Backup) | the data is read | PSO structure invalid |
| J | genuine 1.6% save, 1 byte changed | **Load failed** | integrity is checked | CHKS invalid |
| **K1** | PC data, correct header and CHKS | **38.4%, 36/69 missions** | works | — |

The trap: the original PS5 save was from just after the prologue, the default starting state. When the game rejects the data silently, it falls back to that same state, so most "accepted" tests were really rejections. Test J was decisive because it used a genuine 1.6% save, which doesn't look like the default, so the failure finally showed. That save also came out of a round-trip: loading a test, saving in game and diffing the result showed the game had thrown the PC values away.

A full power cycle (test G after reboot) ruled out the PS5 serving a stale copy from its save-data memory cache.

## 5. Structure findings along the way

- **`PMAP`** is a directory of the `PSIN` block: 41 entries of (hash, offset, 0, length).
- **`PSCH`** is a schema of 434 (hash, offset) entries, which makes PSO data partly self-describing.
- Between the PC and PS5 builds, only 5 of the 41 sub-blocks differ in size, adding 0x570 bytes in total. One of them is the radio-station table, which has one extra station on PC.
- Block sizes also vary between saves made by the same build, because they hold variable-length arrays.

In the end none of this needed converting. Given a valid header and checksum, the old PS5 build reads the newer PC layout through the schema. When it re-saves, it writes the data back in its own layout; the 300 KB block, for example, returns to `0x4b600`.

## 6. Reverse engineering `eboot.bin`

`eboot.bin` is a SELF with 12 segments, nothing encrypted or compressed. The ELF sits at `0x1a0`. I mapped segments to virtual addresses from the program headers, then:

1. **Code that uses the PS5 key:** scanned the code for RIP-relative references to the key's address (VA `0x3f07020`) and found five sites. They identified the AES routines: key setup `0x2b0c030`, encrypt `0x2b0d150`, decrypt `0x2b0d030`, and a decrypt-by-key-id function `0x2b0d410`, where id `0xffffff6` selects the save key.
2. **Serializer:** `0x2b7de60` writes `PSIN` at offset 0 of its buffer. So whatever sits before `PSIN` in the file belongs to an outer layer. The save path at `0x14cd4d0` (`SGV2` compression plus encryption) turned out to be the cloud-save route, not the local one.
3. **Local save code:** found by string search. `SGTA5001…` immediates led to the load dispatcher at `0x2b40770`. It compares the filename with `SGTA50015` (the autosave), `SBGTA50015` and `PROFILE`, then starts a `[RAGE] LoadThread`.
4. **Autosave loader:** `0x2b3ed90` reads the first `0x114` bytes of save memory, which is what showed the header is `0x114` bytes, and calls the header parser `0x2b3bdd0`. The parser reads version, title, `0x104`, `0x108` and `0x10c`, and accepts the save only if `0x2b3bf60(title) == [0x104]`.
5. **`0x2b3bf60`** is joaat with seed `0x9DBEE6` over 256 bytes, using `movsx`, i.e. **signed** chars. It matches all four saves, including the PC save's own 4-byte field.
6. **CHKS:** the PSO chunk parser (around `0x2b72e3d`) handles the `CHKS` tag. Earlier I'd tried recovering a joaat seed for it, which failed because I used unsigned bytes. Joaat's steps can each be run backwards, so given data and an output the seed can be computed. Redone with signed bytes, it gives the same seed, **`0x3FAC7125`**, on all four saves when the CHKS length and checksum fields are zeroed.

## 7. Verification

- Rebuilding both genuine PS5 autosaves from their decrypted data reproduces them byte for byte.
- The converted 38.4% save loads on the PS5 with matching stats and the mission checklist at 36/69.
- The save the game then wrote itself checks out with the same rules. Its only difference from a rebuild is 669 leftover bytes past the end of the data: the game rewrites only the bytes it needs.

## Lessons

- **Check what an "accepted" load contains.** A save that looks like the default state can't tell success from a silent fallback.
- **ECB doesn't care where you start.** Decrypting from the wrong offset that sits at the same position within a 16-byte block still yields plausible data.
- **Signed vs unsigned bytes in a hash** can be the whole difference between "no match" and "solved".
- **Read the loader before trusting black-box tests.** One look at the header parser explained a dozen confusing console results.
