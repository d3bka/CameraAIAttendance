"""Optional RTSP motion signal source. It never identifies people or saves frames."""
import argparse
import logging
import os
import time
import uuid
from datetime import datetime, timezone

try:
    import cv2
    import numpy as np
    import requests
except ImportError:
    raise SystemExit('Install optional RTSP requirements: pip install -r requirements-rtsp.txt')

LOG=logging.getLogger('rtsp_motion_bridge')


def movement_score(previous, current, threshold=25):
    """Fraction of significantly changed pixels. Very simple signal, NOT person detection."""
    delta=cv2.absdiff(previous,current)
    _,mask=cv2.threshold(delta,threshold,255,cv2.THRESH_BINARY)
    mask=cv2.morphologyEx(mask,cv2.MORPH_OPEN,np.ones((3,3),np.uint8))
    return int(cv2.countNonZero(mask))


def run(args):
    rtsp_url=os.environ.get('ATTENDANCE_RTSP_URL')
    token=os.environ.get('ATTENDANCE_INGEST_TOKEN')
    if not rtsp_url or not token:
        raise SystemExit('Set ATTENDANCE_RTSP_URL and ATTENDANCE_INGEST_TOKEN in environment')
    if not rtsp_url.lower().startswith(('rtsp://','rtsps://')):
        raise SystemExit('ATTENDANCE_RTSP_URL must be an RTSP(S) URL')
    api=args.api.rstrip('/')+'/api/ingest'
    last_sent=0
    cv2.setNumThreads(1)
    while True:
        capture=cv2.VideoCapture(rtsp_url)
        try:
            if not capture.isOpened():
                LOG.warning('Could not connect to RTSP stream; retrying')
                time.sleep(5)
                continue
            LOG.info('RTSP stream connected for source %s',args.camera_key)
            background=None
            last_sample=0
            while True:
                ok,frame=capture.read()
                if not ok:break
                now=time.monotonic()
                if now-last_sample < args.interval:continue
                last_sample=now
                gray=cv2.cvtColor(cv2.resize(frame,(480,270)),cv2.COLOR_BGR2GRAY)
                gray=cv2.GaussianBlur(gray,(9,9),0)
                if background is None:
                    background=gray.astype('float32')
                    continue
                score=movement_score(cv2.convertScaleAbs(background),gray)
                cv2.accumulateWeighted(gray,background,.04)
                if score < args.min_changed_pixels or now-last_sent < args.cooldown:
                    continue
                last_sent=now
                payload={'camera_key':args.camera_key,'event_uid':'rtsp-'+uuid.uuid4().hex,
                         'event_type':'motion','verification_method':'none',
                         'observed_at':datetime.now(timezone.utc).isoformat()}
                try:
                    response=requests.post(api,json=payload,headers={'X-Ingest-Token':token},timeout=8)
                    if response.status_code>=400:LOG.warning('API rejected event: HTTP %s',response.status_code)
                    else:LOG.info('Motion event: %s changed pixels',score)
                except requests.RequestException:
                    LOG.warning('Unable to send motion event to local API')
        finally:
            capture.release()
        LOG.warning('RTSP stream lost, reconnect in 5 sec')
        time.sleep(5)


def main():
    parser=argparse.ArgumentParser(description='RTSP motion -> unverified camera events')
    parser.add_argument('--camera-key',required=True)
    parser.add_argument('--api',default='http://127.0.0.1:8000')
    parser.add_argument('--interval',type=float,default=1.0,help='Sampling period, seconds')
    parser.add_argument('--min-changed-pixels',type=int,default=8000,help='Motion sensitivity, 480x270 frame')
    parser.add_argument('--cooldown',type=float,default=20.0,help='Minimum seconds between events')
    args=parser.parse_args()
    if args.interval < .1 or args.cooldown<1 or args.min_changed_pixels<100:
        parser.error('Motion filter arguments outside supported range')
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    run(args)

if __name__=='__main__':main()
