"""Optional Hikvision ISAPI listener -> local attendance API.

Requires: pip install requests. This adapter never stores camera passwords or raw images.
Configure permitted successful-access minor codes from YOUR device's documentation.
"""
import argparse
import hashlib
import json
import logging
import os
import re
import time
from datetime import datetime
from xml.etree import ElementTree

try:
    import requests
    from requests.auth import HTTPDigestAuth
except ImportError:
    raise SystemExit('Install the optional bridge dependency: pip install requests')

LOG=logging.getLogger('isapi_bridge')
XML_CLOSE=re.compile(rb'</(?:[A-Za-z][\w.-]*:)?EventNotificationAlert\s*>')
XML_OPEN=re.compile(rb'<(?:[A-Za-z][\w.-]*:)?EventNotificationAlert(?:\s|>)')


def remove_ns(tag):
    return tag.rsplit('}',1)[-1].rsplit(':',1)[-1]


def xml_to_dict(element):
    children=list(element)
    if not children:return (element.text or '').strip()
    result={}
    for child in children:
        name=remove_ns(child.tag)
        value=xml_to_dict(child)
        if name in result:
            if not isinstance(result[name],list):result[name]=[result[name]]
            result[name].append(value)
        else:result[name]=value
    return result


def decode_event(blob):
    """Parse one complete JSON or XML alert, strip namespaces, ignore unrelated data."""
    if len(blob)>256*1024:raise ValueError('Hikvision event is too large')
    if blob.lstrip().startswith(b'{'):
        obj=json.loads(blob)
        event=obj.get('EventNotificationAlert',obj)
        return event if isinstance(event,dict) else None
    root=ElementTree.fromstring(blob)
    if remove_ns(root.tag)!='EventNotificationAlert':return None
    return xml_to_dict(root)


def event_stream(chunks):
    """Minimal streaming frame decoder for XML and JSON multipart event bodies.

    Supports complete one-line JSON blocks and XML alerts with variable namespace.
    Does not attempt to parse image parts, which are intentionally discarded.
    """
    buffer=b''
    for chunk in chunks:
        if not chunk:continue
        buffer+=chunk
        # Some devices emit one-line JSON in a multipart stream.
        while True:
            start=XML_OPEN.search(buffer)
            if not start:break
            end=XML_CLOSE.search(buffer,start.start())
            if not end:break
            data=buffer[start.start():end.end()]
            buffer=buffer[end.end():]
            try:
                ev=decode_event(data)
                if ev:yield ev
            except (ValueError,ElementTree.ParseError) as exc:
                LOG.warning('Invalid XML event: %s',type(exc).__name__)
        # JSON alerts can arrive with Content-Length and newlines.
        while b'\n' in buffer and not XML_OPEN.search(buffer):
            line,buffer=buffer.split(b'\n',1)
            stripped=line.strip().strip(b'\r')
            if not stripped.startswith(b'{'):continue
            try:
                ev=decode_event(stripped)
                if ev:yield ev
            except (ValueError,json.JSONDecodeError):pass
        if len(buffer)>512*1024:
            LOG.warning('Discarding oversized/unparseable streaming frame')
            buffer=b''


def normalized_event(evt,camera_key,allowed_minor):
    """Never grant attendance from motion or an unrecognized controller event."""
    kind=str(evt.get('eventType','')).strip()
    metadata=evt.get('AccessControllerEvent',{})
    if not isinstance(metadata,dict):metadata={}
    code=str(metadata.get('employeeNoString') or metadata.get('employeeNo') or '').strip()
    minor=str(metadata.get('subEventType') or metadata.get('minorEventType') or metadata.get('minor') or '').strip()
    uid_seed=json.dumps(evt,sort_keys=True,ensure_ascii=False,default=str)
    eid=hashlib.sha256((camera_key+uid_seed).encode('utf8')).hexdigest()[:48]
    timestamp=evt.get('dateTime')
    payload={'camera_key':camera_key,'event_uid':eid,'event_type':'device_event',
             'verification_method':'none'}
    if isinstance(timestamp,str) and timestamp.strip():payload['observed_at']=timestamp
    if kind.lower() in ('vmd','motion','linedetection','fielddetection'):
        payload['event_type']='motion'
    elif kind=='AccessControllerEvent' and code and minor in allowed_minor and allowed_minor:
        payload['event_type']='access_granted'
        payload['person_code']=code
        payload['verification_method']='card'  # exact modality cannot be inferred from these fields
    return payload


def listen(args):
    password=os.environ.get('HIK_PASSWORD')
    ingest_token=os.environ.get('ATTENDANCE_INGEST_TOKEN')
    if not password or not ingest_token:raise SystemExit('Set HIK_PASSWORD and ATTENDANCE_INGEST_TOKEN environment variables')
    if not args.allowed_minor:
        LOG.warning('No access-success minor event codes configured. All access alerts will remain UNVERIFIED.')
    session=requests.Session()
    auth=HTTPDigestAuth(args.user,password)
    endpoint=f'{args.scheme}://{args.host}:{args.port}/ISAPI/Event/notification/alertStream'
    api=args.api.rstrip('/')+'/api/ingest'
    while True:
        try:
            with session.get(endpoint,auth=auth,stream=True,timeout=(5,60)) as resp:
                resp.raise_for_status()
                LOG.info('Connected to event stream for %s',args.camera_key)
                for event in event_stream(resp.iter_content(chunk_size=4096)):
                    payload=normalized_event(event,args.camera_key,set(args.allowed_minor))
                    try:
                        r=requests.post(api,json=payload,headers={'X-Ingest-Token':ingest_token},timeout=10)
                        if r.status_code>=400:LOG.warning('Ingestion rejected: HTTP %d %s',r.status_code,r.text[:150])
                        else:LOG.info('Stored %s: %s',payload['event_type'],r.json().get('verified'))
                    except requests.RequestException as exc:
                        LOG.warning('Local API unavailable; event may be lost: %s',type(exc).__name__)
        except requests.RequestException as exc:
            LOG.warning('Device stream disconnected: %s; retrying',type(exc).__name__)
        time.sleep(5)


def main():
    parser=argparse.ArgumentParser(description='One-device Hikvision ISAPI event bridge')
    parser.add_argument('--host',required=True,help='Camera/NVR IP or hostname')
    parser.add_argument('--port',type=int,default=80)
    parser.add_argument('--scheme',choices=['http','https'],default='http')
    parser.add_argument('--user',default='admin')
    parser.add_argument('--camera-key',required=True,help='Existing camera_key from admin panel')
    parser.add_argument('--allowed-minor',action='append',default=[],help='Verified access-success minor event code on this device (repeatable)')
    parser.add_argument('--api',default='http://127.0.0.1:8000')
    args=parser.parse_args()
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    listen(args)

if __name__=='__main__':main()
