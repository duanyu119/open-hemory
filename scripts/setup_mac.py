#!/usr/bin/env python3
"""Create private local pairing files and optional launchd jobs; never publish a port."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import secrets
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
DATA = Path.home() / 'Library/Application Support/HemoryLocal'

def private_write(path, content):
    path.write_bytes(content if isinstance(content, bytes) else content.encode())
    path.chmod(0o600)

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--install', action='store_true', help='Install only the user launchd receiver; no cloud worker')
    p.add_argument('--data-dir', type=Path, default=DATA)
    p.add_argument('--host', default='127.0.0.1', choices=['127.0.0.1', '0.0.0.0'])
    p.add_argument('--port', type=int, default=8765)
    a = p.parse_args()
    os.umask(0o077)
    data = a.data_dir.expanduser().resolve()
    data.mkdir(parents=True, exist_ok=True, mode=0o700)
    data.chmod(0o700)
    private = data/'private'
    private.mkdir(exist_ok=True, mode=0o700)
    local_name = subprocess.check_output(['scutil', '--get', 'LocalHostName'], text=True).strip() + '.local'
    token_file = private/'receiver-token.txt'
    if not token_file.exists():
        private_write(token_file, secrets.token_urlsafe(48)+'\n')
    cert, key = private/'receiver-cert.pem', private/'receiver-key.pem'
    if not cert.exists() or not key.exists():
        openssl = shutil.which('openssl')
        if not openssl:
            raise SystemExit('openssl is required')
        subprocess.run([openssl,'req','-x509','-newkey','rsa:2048','-nodes','-days','365',
                        '-keyout',str(key),'-out',str(cert),'-subj','/CN='+local_name,
                        '-addext','subjectAltName=DNS:'+local_name+',DNS:localhost,IP:127.0.0.1',
                        '-addext','extendedKeyUsage=serverAuth',
                        '-addext','keyUsage=critical,digitalSignature,keyEncipherment',
                        '-addext','basicConstraints=critical,CA:FALSE'],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        key.chmod(0o600)
        cert.chmod(0o600)
    der = subprocess.check_output(['openssl','x509','-in',str(cert),'-outform','DER'])
    # Contains secrets: transfer privately to your own iPhone; never commit this file.
    pairing = {'mac_url':f'https://{local_name}:{a.port}', 'token':token_file.read_text().strip(),
               'certificate_sha256':hashlib.sha256(der).hexdigest(), 'tls_mode':'pin',
               'cf_access_client_id':'', 'cf_access_client_secret':'', 'allow_mobile_data':False}
    private_write(private/'mac-pairing.json', json.dumps(pairing, ensure_ascii=False, indent=2)+'\n')
    print('Private Mac settings created; credentials are not printed.')
    print('Local receiver URL:', pairing['mac_url'])
    print('Remote access: not configured. No router or tunnel was changed.')
    if not a.install:
        return
    logs = data/'logs'
    logs.mkdir(exist_ok=True)
    agents = Path.home()/'Library/LaunchAgents'
    agents.mkdir(exist_ok=True)
    common = [sys.executable, str(ROOT/'mac/hemory_local.py'), '--data-dir',str(data)]
    commands = {'receiver': common+['server','--host',a.host,'--port',str(a.port),
                '--cert',str(cert),'--key',str(key),'--token-file',str(token_file)]}
    for name, command in commands.items():
        label = 'org.open-hemory.'+name
        path = agents/(label+'.plist')
        job = {'Label':label,'ProgramArguments':command,'RunAtLoad':True,'KeepAlive':True,
               'WorkingDirectory':str(ROOT),'ThrottleInterval':30,
               'EnvironmentVariables':{'PATH':'/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin'},
               'StandardOutPath':str(logs/(name+'.log')),'StandardErrorPath':str(logs/(name+'.error.log'))}
        private_write(path, plistlib.dumps(job))
        subprocess.run(['launchctl','bootout',f'gui/{os.getuid()}/{label}'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        subprocess.run(['launchctl','bootstrap',f'gui/{os.getuid()}',str(path)],check=True)
        print('Installed local background service:',label)

if __name__ == '__main__':
    main()
