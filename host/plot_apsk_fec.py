"""Scientific APSK full-pipeline figures from four qualified60s digital trials.

Every plotted point requires revalidated finite GPIO data, continuous real-frame
history, live on-chip PV-SPI, runtime FEC accounting and matching source, image,
archive and verified-flash evidence. This does not qualify an AFE or RF link.
"""
from __future__ import annotations

import argparse
import copy
import gzip
import hashlib
import io
import json
import math
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import zlib

import apsk_fec_trial as trial

ROOT = Path(__file__).resolve().parents[1]
CAMPAIGN = ROOT/'results/apsk-fec/2026-10-15'
CASES = ((16, 128000), (16, 150000), (32, 128000), (32, 150000))
REFERENCE_FILES = ('host/apsk_fec_trial.py', 'host/dual_spi.py', 'host/iqbench.py',
                   'host/dual_trial.py', 'host/iq16out_trial.py', 'host/iq32out_trial.py',
                   'host/test_iq16chunk.py', 'host/test_iq16native8.py',
                   'host/test_iq32native8.py', 'host/cm5/pv_spi_tx.py',
                   'reference/dvbs2/dvbs2.py', 'reference/iqlut.py', 'reference/gen_coeffs.py')
REQUIRED_FIRMWARE = ('firmware/CMakeLists.txt', 'firmware/git_rev.cmake',
                     'firmware/src/bench_apsk_fec.c', 'firmware/src/dvbs2_apsk.c',
                     'firmware/src/dvbs2.c', 'firmware/src/iqout.c')


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def relative(path):
    path = path.resolve()
    return str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path)


def json_file(path):
    require(path.is_file(), 'missing evidence: '+str(path))
    value = json.loads(path.read_text())
    require(type(value) is dict, 'evidence must be a JSON object: '+str(path))
    return value


def validate_row(row, modulation, clock, git):
    require(row.get('passed') is True and row.get('classification') == 'clean' and
            row.get('faults') == [] and not row.get('error'), 'run is not positively qualified')
    cpw = row.get('cpw')
    require(type(cpw) is int and 3 <= cpw <= 32, 'invalid output divisor')
    faults = trial.stream_faults(row, modulation, clock, cpw, 60000, git)
    require(not faults, 'full-pipeline accounting failed: '+', '.join(faults))
    require(60000000 <= row['elapsed_us'] <= 60003000, 'incomplete60s qualification')
    require(row.get('cap_physical_iq_ok') is True and not row.get('cap_error'),
            'physical capture validation missing')


def validate_capture(row, lines):
    checked = trial.validate_capture(row, lines)
    require(checked.get('cap_transport_ok') is True and checked.get('cap_aligned') is True and
            checked.get('cap_physical_iq_ok') is True and checked.get('cap_mismatches') == 0 and
            checked.get('cap_header_covered') is True and checked.get('cap_checked_bus_words') == 8192,
            'independent finite/header capture mismatch')
    for field in ('cap_global_bus_offset', 'cap_reference_first_frame', 'cap_start_search_bus_words'):
        require(row.get(field) == checked.get(field), 'independent capture alignment changed: '+field)
    return checked


def archived_images(path, hashes):
    """Read the two immutable images without extracting anything to disk."""
    try:
        require(path.stat().st_size <= 16*1024*1024, 'image archive exceeds bounded size')
        with gzip.open(path, 'rb') as compressed:
            payload = compressed.read(32*1024*1024+1)
        require(1024 <= len(payload) <= 32*1024*1024 and len(payload) % 512 == 0 and
                payload[-1024:] == bytes(1024), 'malformed image archive padding/size')
        with tarfile.open(fileobj=io.BytesIO(payload), mode='r:') as archive:
            members = archive.getmembers()
            names = ('iqbench.uf2', 'iqbench.elf')
            require(len(members) == 2 and {member.name for member in members} == set(names) and
                    all(member.isfile() and 0 < member.size <= 16*1024*1024 for member in members),
                    'image archive has duplicate, missing or unexpected members')
            end = max(member.offset_data+((member.size+511)//512)*512 for member in members)
            require(len(payload)-end >= 1024 and not any(payload[end:]),
                    'unexpected data after archived images')
            actual = {}
            for member in members:
                data = archive.extractfile(member).read()
                require(len(data) == member.size, 'truncated archived image: '+member.name)
                actual[member.name] = hashlib.sha256(data).hexdigest()
            require(all(actual[name] == hashes.get('build/'+name) for name in names),
                    'archived image differs from build manifest')
            return actual
    except (OSError, EOFError, tarfile.TarError, zlib.error) as exc:
        raise ValueError('malformed firmware image archive') from exc


def build_evidence(folder, manifest):
    hashes = manifest.get('sha256', {})
    require(type(hashes) is dict and all(type(hashes.get(name)) is str for name in ('iqbench.uf2', 'iqbench.elf')),
            'campaign image hashes missing')
    matches = []
    for path in folder.parent.glob('build-*/manifest.json'):
        build = json_file(path)
        if all(build.get('sha256', {}).get('build/'+name) == hashes[name] for name in ('iqbench.uf2', 'iqbench.elf')):
            matches.append((path, build))
    require(len(matches) == 1, 'requires one uniquely matching archived build')
    path, build = matches[0]
    require(build.get('git_rev') == '#define GIT_REV "'+manifest['expected_firmware_git']+'"',
            'build/campaign firmware identity mismatch')
    archive_path, flash_path, build_log, binding_path = (path.parent/name for name in
        ('firmware-source.tar.gz', 'flash.txt', 'build.txt', 'flash-image.json'))
    require(all(p.is_file() for p in (archive_path, flash_path, build_log, binding_path)),
            'source/build/flash evidence incomplete')
    expected = {name: value for name, value in build['sha256'].items() if name.startswith('firmware/')}
    require(all(name in expected for name in REQUIRED_FIRMWARE), 'critical firmware source evidence missing')
    with tarfile.open(archive_path, 'r:gz') as archive:
        entries = [entry for entry in archive.getmembers() if entry.isfile()]
        require(len({entry.name for entry in entries}) == len(entries), 'duplicate archived source names')
        actual = {entry.name: hashlib.sha256(archive.extractfile(entry).read()).hexdigest() for entry in entries}
    require(actual == expected, 'archived source differs from build manifest')
    recorded = {name: value for name, value in hashes.items() if name.startswith('firmware/')}
    require(recorded == actual, 'campaign source differs from its archived build')
    files = [path, archive_path, build_log, flash_path, binding_path]
    image_archive = path.parent/'firmware-images.tar.gz'
    shared_images = archived_images(image_archive, build['sha256']) if image_archive.exists() else None
    if shared_images is not None:
        files.append(image_archive)
    image_sources = {}
    for name in ('iqbench.uf2', 'iqbench.elf'):
        binary = folder/name
        if binary.exists():
            require(binary.is_file() and sha(binary) == hashes[name], 'campaign image hash mismatch: '+name)
        else:
            require(shared_images is not None, 'campaign image and shared archive missing: '+name)
        if shared_images is not None:
            require(shared_images[name] == hashes[name], 'archived image differs from campaign: '+name)
            image_sources[name] = dict(archive=relative(image_archive), member=name, sha256=shared_images[name])
        else:
            files.append(binary)
            image_sources[name] = dict(path=relative(binary), sha256=hashes[name])
    flash = flash_path.read_text()
    require('Verifying Flash:' in flash and '\n  OK\n' in flash and
            'The device was rebooted to start the application.' in flash, 'verified flash record missing')
    binding = json_file(binding_path)
    require(binding.get('exit_code') == 0 and binding.get('uf2_sha256') == hashes['iqbench.uf2'] and
            binding.get('build_manifest') == path.name and binding.get('build_manifest_sha256') == sha(path) and
            binding.get('flash_log') == flash_path.name and binding.get('flash_log_sha256') == sha(flash_path),
            'verified flash image/build/log association differs')
    command = binding.get('command_argv')
    require(type(command) is list and all(type(part) is str for part in command) and
            len(command) >= 5 and Path(command[0]).name == 'picotool' and 'load' in command and
            '-v' in command and '-x' in command and
            any(Path(part).name == 'iqbench.uf2' for part in command) and
            type(binding.get('device_serial')) is str and bool(binding['device_serial']),
            'flash command/device evidence incomplete')
    return dict(path=relative(path.parent), source_files=len(actual),
                sha256={relative(p): sha(p) for p in files}, archived_firmware_sha256=actual,
                image_sources=image_sources,
                flash_input_uf2_sha256=binding['uf2_sha256'], device_serial=binding['device_serial'])


def load_campaign(folder, modulation, clock):
    folder = folder.resolve()
    manifest_path, summary_path, runs_path = (folder/name for name in ('manifest.json', 'summary.json', 'runs.jsonl'))
    manifest, summary = json_file(manifest_path), json_file(summary_path)
    require(runs_path.is_file(), 'campaign runs missing')
    require(summary.get('passed') is True and summary.get('failures') == [] and summary.get('fatal') is None,
            'campaign summary is not clean')
    cases = manifest.get('cases')
    require(type(cases) is list and len(cases) == 1 and type(cases[0]) is list and len(cases[0]) == 3 and
            cases[0][:2] == [modulation, clock] and type(cases[0][2]) is int,
            'campaign selection is not one requested mode/clock')
    require(manifest.get('ms') == 60000 and manifest.get('expect_deadline_fault') is False,
            'campaign is not a positive60s trial')
    git = manifest.get('expected_firmware_git')
    require(type(git) is str and bool(git), 'expected firmware identity missing')
    references = {name: value for name, value in manifest.get('sha256', {}).items()
                  if name.startswith(('host/', 'reference/'))}
    require(all(name in references for name in REFERENCE_FILES), 'independent reference evidence incomplete')
    for name, value in references.items():
        target = ROOT/name
        require(target.is_file() and sha(target) == value, 'independent reference source drift: '+name)
    evidence = build_evidence(folder, manifest)
    selected = []
    for number, line in enumerate(runs_path.read_text().splitlines(), 1):
        row = json.loads(line)
        require(type(row) is dict, 'run record is not a JSON object')
        require(not row.get('exception'), 'campaign includes a host exception')
        if row.get('cmd') != 'benchapskfec':
            continue
        validate_row(row, modulation, clock, git)
        require(row['cpw'] == cases[0][2], 'run divisor differs from campaign')
        filename = row.get('capture_file')
        require(type(filename) is str and Path(filename).name == filename, 'invalid capture filename')
        capture = folder/filename
        require(capture.is_file(), 'capture file unavailable')
        checked = validate_capture(row, capture.read_text().splitlines())
        selected.append(dict(row=row, source=dict(path=relative(runs_path), line=number, sha256=sha(runs_path)),
                             capture=dict(path=relative(capture), sha256=sha(capture), validation=checked)))
    require(len(selected) == 1, 'requires exactly one clean60s run per campaign')
    return dict(run=selected[0], manifest=dict(path=relative(manifest_path), sha256=sha(manifest_path)),
                summary=dict(path=relative(summary_path), sha256=sha(summary_path)),
                build=evidence, reference_sha256=references)


def select_campaigns(folders):
    require(len(folders) == 4, 'requires all four modulation/clock campaigns')
    selected = [load_campaign(folder, modulation, clock) for folder, (modulation, clock) in zip(folders, CASES)]
    require(len({entry['build']['device_serial'] for entry in selected}) == 1, 'campaigns use different devices')
    return selected


def render(folders, out):
    selected = select_campaigns(folders)
    os.environ.setdefault('MPLCONFIGDIR', str(Path(tempfile.gettempdir())/'yapogee-mpl-cache'))
    sys.path.insert(0, str(ROOT/'reference'))
    from plotstyle import BLUE, RED, WIDTH, dots, plt, save_figure
    fig, axes = plt.subplots(1, 2, figsize=(WIDTH, 2.65))
    for modulation, color, marker, linestyle in ((16, BLUE, 's', '-'), (32, RED, 'o', '--')):
        rows = [entry['run']['row'] for entry in selected if entry['run']['row']['modulation'] == modulation]
        for ax, field in zip(axes, ('sym_rate', 'bb_payload_bps')):
            x, y = [r['clk_hz']/1e6 for r in rows], [r[field]/1e6 for r in rows]
            dots(ax, x, y, color, f'{modulation}APSK', marker=marker, linestyle=linestyle)
            for clock, value in zip(x, y):
                ax.annotate(f'{value:.3g}', (clock, value), xytext=(0, 6 if modulation == 16 else -11),
                            textcoords='offset points', ha='center', fontsize=7.5, color=color)
    for ax in axes:
        ax.set(xlim=(123, 155), xticks=[128, 150], xlabel='System clock (MHz)')
        ax.set_ylim(0, ax.get_ylim()[1]*1.15)
        ax.grid(axis='y')
    axes[0].set(ylabel='Symbol rate (Msym/s)', title='(a) Sustained digital output')
    axes[1].set(ylabel='BB payload capacity (Mb/s)', title='(b) Useful frame capacity')
    axes[0].legend(loc='lower right')
    out.mkdir(parents=True, exist_ok=True)
    save_figure(fig, out/'apsk_fec.png')
    record = dict(schema='yapogee-apsk-fec-figures-v1', selected=selected,
                  caption='60 s; normal 5/6, pilots; runtime SPI, FEC, mapping and shaping. '
                          'Finite GPIO capture checked; on-chip payload emulation; no AFE/RF qualification.',
                  formulas=dict(symbol_rate='clk_hz/(4*cpw)',
                                bb_payload_capacity='symbol_rate*(kbch-80)/frame_symbols'),
                  scope='One RP2350, two cores, stock clocks/voltage, USB only; on-chip PV-SPI source, '
                        'real GCS/BB/BCH/LDPC, standard16/32APSK PL framing, continuous native L8/N2 FIR, DMA and PIO.',
                  capacity='BB data-field capacity after80-bit BBHEADER; includes any GCS packet headers/padding '
                           'inside that field. It is not application goodput or external SPI qualification.',
                  selection='Exactly one clean60s trial per modulation and clock in the four supplied campaigns; '
                            'these qualified points do not assert a global maximum.',
                  uncertainty='Finite-run deterministic output-clock rates; no inferred confidence intervals or unmeasured headroom.',
                  validation='All8192 contiguous GPIO halfwords, including one complete90-symbol PLHEADER, '
                             'revalidated at one bounded offset in finite real FEC frames; no cyclic replay.',
                  style=dict(path='reference/plotstyle.py', sha256=sha(ROOT/'reference/plotstyle.py')),
                  script_sha256=sha(Path(__file__)), capture_validator_sha256=sha(ROOT/'host/apsk_fec_trial.py'),
                  output_sha256={f'apsk_fec.{ext}': sha(out/f'apsk_fec.{ext}') for ext in ('png', 'svg', 'pdf')})
    (out/'selection.json').write_text(json.dumps(record, indent=2)+'\n')
    print(json.dumps(dict(output=str(out), figures=['apsk_fec.png', 'apsk_fec.svg', 'apsk_fec.pdf'],
                          provenance='selection.json', qualified_runs=4)))


def self_test():
    """Synthetic campaigns test the evidence chain without requiring a Pico or soaks."""
    def rejected(action, label):
        nonlocal negatives
        try:
            action()
        except (ValueError, FileNotFoundError):
            negatives += 1
        else:
            raise AssertionError('accepted '+label)

    negatives = 0
    with tempfile.TemporaryDirectory(prefix='apsk-fec-plot-test-') as directory:
        temp = Path(directory)
        build = temp/'build-1'; build.mkdir()
        source_bytes = {name: (ROOT/name).read_bytes() for name in REQUIRED_FIRMWARE}
        source_hashes = {name: hashlib.sha256(data).hexdigest() for name, data in source_bytes.items()}
        images = {'iqbench.uf2': b'synthetic UF2 evidence fixture', 'iqbench.elf': b'synthetic ELF evidence fixture'}
        image_hashes = {name: hashlib.sha256(data).hexdigest() for name, data in images.items()}
        build_manifest = dict(git_rev='#define GIT_REV "offline-self-test"',
                              sha256=dict(source_hashes, **{'build/'+name: value for name, value in image_hashes.items()}))
        (build/'manifest.json').write_text(json.dumps(build_manifest))
        with tarfile.open(build/'firmware-source.tar.gz', 'w:gz') as archive:
            for name, data in source_bytes.items():
                member = tarfile.TarInfo(name); member.size = len(data)
                archive.addfile(member, io.BytesIO(data))
        (build/'build.txt').write_text('synthetic successful build fixture\n')
        (build/'flash.txt').write_text('Verifying Flash:\n  OK\nThe device was rebooted to start the application.\n')
        binding = dict(command_argv=['picotool','load','-v','synthetic/iqbench.uf2','-x'], exit_code=0,
                       uf2_sha256=image_hashes['iqbench.uf2'], build_manifest='manifest.json',
                       build_manifest_sha256=sha(build/'manifest.json'), flash_log='flash.txt',
                       flash_log_sha256=sha(build/'flash.txt'), device_serial='synthetic-device')
        (build/'flash-image.json').write_text(json.dumps(binding))
        references = {name: sha(ROOT/name) for name in REFERENCE_FILES}
        folders = []
        for modulation, clock in CASES:
            cpw = 16 if modulation == 16 else 24
            row, lines = trial.metadata_fixture(modulation, clock, cpw, 60000)
            row.update(git='offline-self-test', passed=True, faults=[], classification='clean', capture_file='capture.txt')
            folder = temp/f'qualified-{modulation}-{clock}'; folder.mkdir(); folders.append(folder)
            (folder/'runs.jsonl').write_text(json.dumps(row)+'\n')
            (folder/'capture.txt').write_text('\n'.join(lines)+'\n')
            (folder/'summary.json').write_text(json.dumps(dict(passed=True, failures=[], fatal=None)))
            manifest = dict(cases=[[modulation,clock,cpw]], ms=60000, expect_deadline_fault=False,
                            expected_firmware_git='offline-self-test', sha256=dict(references, **source_hashes, **image_hashes))
            (folder/'manifest.json').write_text(json.dumps(manifest))
            for name, data in images.items():
                (folder/name).write_bytes(data)
        selected = select_campaigns(folders)
        row = selected[0]['run']['row']
        for field, value in (('ms',3000), ('passed',False), ('classification','failed'), ('errors',2),
                             ('cap_header_covered',False), ('cap_checked_bus_words',8191), ('encoder_busy',math.nan),
                             ('fe_crc',[0,0,0,0]), ('mapped_symbols',0), ('bb_payload_bps',math.nan)):
            bad = copy.deepcopy(row); bad[field] = value
            rejected(lambda: validate_row(bad,16,128000,'offline-self-test'), field)
        rejected(lambda: select_campaigns(folders[:3]), 'incomplete campaign matrix')
        capture = folders[0]/'capture.txt'; original_capture = capture.read_text()
        capture.write_text('\n'.join(original_capture.splitlines()[1:]))
        rejected(lambda: load_campaign(folders[0],16,128000), 'truncated capture')
        capture.write_text(original_capture)
        words = trial.parse_capture(original_capture.splitlines())
        bus = words.view('<u2').copy(); bus[5000] ^= 1
        info, lines = trial.capture_lines(bus)
        bad = dict(row, **info)
        rejected(lambda: validate_capture(bad,lines), 'wrong waveform with recomputed transport CRC')
        for field in ('uf2_sha256','flash_log_sha256','build_manifest_sha256'):
            broken = dict(binding, **{field:'0'*64})
            (build/'flash-image.json').write_text(json.dumps(broken))
            rejected(lambda: load_campaign(folders[0],16,128000), 'flash binding '+field)
        (build/'flash-image.json').write_text(json.dumps(binding))
        for field, value in (('exit_code',1), ('command_argv',['picotool','load','synthetic/iqbench.uf2','-x'])):
            (build/'flash-image.json').write_text(json.dumps(dict(binding, **{field:value})))
            rejected(lambda: load_campaign(folders[0],16,128000), 'unverified flash '+field)
        original_flash = (build/'flash.txt').read_text()
        (build/'flash.txt').write_text('Flash load failed\n')
        (build/'flash-image.json').write_text(json.dumps(dict(binding, flash_log_sha256=sha(build/'flash.txt'))))
        rejected(lambda: load_campaign(folders[0],16,128000), 'failed flash with matching log hash')
        (build/'flash.txt').write_text(original_flash)
        (build/'flash-image.json').write_text(json.dumps(binding))
        manifest_path = folders[0]/'manifest.json'; original_manifest = manifest_path.read_text()
        for field in ('iqbench.uf2','firmware/src/dvbs2.c','host/dual_spi.py'):
            broken = json.loads(original_manifest); broken['sha256'][field] = '0'*64
            manifest_path.write_text(json.dumps(broken))
            rejected(lambda: load_campaign(folders[0],16,128000), 'campaign hash '+field)
        manifest_path.write_text(original_manifest)
        archived = build/'firmware-source.tar.gz'; original_archive = archived.read_bytes()
        with tarfile.open(archived,'w:gz') as archive:
            member = tarfile.TarInfo('firmware/src/dvbs2.c'); member.size = 1
            archive.addfile(member,io.BytesIO(b'x'))
        rejected(lambda: load_campaign(folders[0],16,128000), 'altered source archive')
        archived.write_bytes(original_archive)
        image_archive = build/'firmware-images.tar.gz'
        def write_images(entries):
            with tarfile.open(image_archive,'w:gz') as archive:
                for name, data in entries:
                    member = tarfile.TarInfo(name); member.size = len(data)
                    archive.addfile(member,io.BytesIO(data))
        write_images(images.items())
        original_images = image_archive.read_bytes()
        for folder in folders:
            for name in images:
                (folder/name).unlink()
        fallback = select_campaigns(folders)
        assert all('archive' in source for entry in fallback for source in entry['build']['image_sources'].values())
        for mutation in ('corrupt_image', 'duplicate', 'unexpected', 'missing', 'malformed'):
            entries = list(images.items())
            if mutation == 'corrupt_image': entries[0] = (entries[0][0], entries[0][1]+b'x')
            elif mutation == 'duplicate': entries.append(entries[0])
            elif mutation == 'unexpected': entries.append(('extra.txt',b'x'))
            elif mutation == 'missing': entries.pop()
            write_images(entries)
            if mutation == 'malformed': image_archive.write_bytes(b'invalid gzip archive')
            rejected(lambda: load_campaign(folders[0],16,128000), 'image archive '+mutation)
        image_archive.write_bytes(original_images)
        (folders[0]/'iqbench.uf2').write_bytes(b'corrupt local image with valid shared archive')
        rejected(lambda: load_campaign(folders[0],16,128000), 'corrupt local image must not fall back')
        (folders[0]/'iqbench.uf2').unlink()
        select_campaigns(folders)
    print(json.dumps(dict(self_test='passed', synthetic_campaigns=4, negative_controls=negatives,
                          capture_observations_per_run=8192, source_archive_and_flash_binding_checked=True,
                          shared_image_archive_fallback_checked=True)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sources', type=Path, nargs=4, default=[CAMPAIGN/f'qualified-{m}-{c//1000}-1' for m,c in CASES],
                        metavar='FOLDER', help='16@128,16@150,32@128,32@150MHz campaign folders')
    parser.add_argument('--out', type=Path, default=ROOT/'results/plots/apsk-fec-2026-10-15')
    parser.add_argument('--self-test', action='store_true')
    args = parser.parse_args()
    if args.self_test:
        self_test()
    else:
        render(args.sources,args.out)


if __name__ == '__main__':
    main()
