"""Export only the two-version project's maintained source and HUD release."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import zipfile


def source_files(root):
    paths = [root/n for n in ['README.md','LICENSE','SECURITY.md','DESIGN.md','启动额度HUD.vbs','.gitignore','.gitattributes']]
    paths += [p for p in (root/'docs').rglob('*') if p.is_file()]
    for pattern in ['*.py','*.txt','*.spec','*.bat','VERSION']:
        paths += list((root/'source').glob(pattern))
    for directory, patterns in [('native',['*.cs','*.dll']),('tests',['test_*.py']),('tools',['*.py','*.ps1'])]:
        for pattern in patterns:
            paths += list((root/'source'/directory).glob(pattern))
    paths += list((root/'source/third_party').rglob('*.txt'))
    return sorted(set(paths))


def package(metadata_only=False):
    root = Path(__file__).resolve().parents[2]
    release, source = root/'release', root/'source'
    binary = release/'CodexQuotaHUD'
    assert (binary/'CodexQuotaHUD.exe').is_file()
    paths = source_files(root)
    manifest = dict(version=(source/'VERSION').read_text().strip(),entrypoint='source/hud_entry.py',
                    files={p.relative_to(root).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in paths})
    (binary/'source-manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    shutil.copy2(root/'docs/使用说明.md',binary/'使用说明.md')
    shutil.copy2(source/'THIRD_PARTY_NOTICES.md',binary/'THIRD_PARTY_NOTICES.md')
    shutil.copy2(root/'LICENSE',binary/'LICENSE')
    shutil.copytree(source/'third_party/licenses',binary/'licenses',dirs_exist_ok=True)
    if metadata_only:
        print('Release metadata and maintained source paths verified:',len(paths))
        return
    outputs = [binary/'CodexQuotaHUD.exe']
    archive_path = release/'CodexQuotaHUD-source.zip'
    with zipfile.ZipFile(archive_path,'w',zipfile.ZIP_DEFLATED) as archive:
        for path in paths: archive.write(path,path.relative_to(root).as_posix())
        archive.writestr('manifest.json',json.dumps(manifest,ensure_ascii=False,indent=2))
    outputs.append(archive_path)
    archive_path = release/'CodexQuotaHUD-windows-x64.zip'
    with zipfile.ZipFile(archive_path,'w',zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(binary.rglob('*')):
            if path.is_file(): archive.write(path,path.relative_to(release).as_posix())
    outputs.append(archive_path)
    for path in outputs[1:]:
        with zipfile.ZipFile(path) as archive: assert archive.testzip() is None
    (release/'SHA256SUMS.txt').write_text(''.join(hashlib.sha256(p.read_bytes()).hexdigest()+'  '+p.relative_to(release).as_posix()+'\n' for p in outputs),encoding='ascii')
    print('Source and Windows packages verified.')


if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--metadata-only',action='store_true')
    package(parser.parse_args().metadata_only)
