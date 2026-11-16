import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import unittest
from isapi_bridge import decode_event, event_stream, normalized_event

class BridgeTest(unittest.TestCase):
    def test_xml(self):
        xml=b'<EventNotificationAlert><eventType>VMD</eventType><dateTime>2026-10-25T10:00:00+05:00</dateTime></EventNotificationAlert>'
        self.assertEqual(decode_event(xml)['eventType'],'VMD')
        data=list(event_stream([xml[:25],xml[25:58],xml[58:]]))
        self.assertEqual(len(data),1)
        self.assertEqual(normalized_event(data[0],'gate_in',set())['event_type'],'motion')
    def test_xml_ns(self):
        xml=b'<EventNotificationAlert xmlns="http://www.isapi.org/ver20/XMLSchema"><eventType>VMD</eventType></EventNotificationAlert>'
        self.assertEqual(decode_event(xml)['eventType'],'VMD')
    def test_namespaced_stream(self):
        xml=b'<h:EventNotificationAlert xmlns:h="urn:hik"><h:eventType>VMD</h:eventType></h:EventNotificationAlert>'
        self.assertEqual(list(event_stream([xml[:49],xml[49:]]))[0]['eventType'],'VMD')
    def test_json(self):
        raw=b'{"EventNotificationAlert":{"eventType":"VMD"}}'
        self.assertEqual(decode_event(raw)['eventType'],'VMD')
        self.assertEqual(len(list(event_stream([b'Content-Type: application/json\n'+raw+b'\n']))),1)
    def test_ambiguous_access_not_verified(self):
        event={'eventType':'AccessControllerEvent','AccessControllerEvent':{'employeeNoString':'STU001','minor':'75'}}
        self.assertEqual(normalized_event(event,'gate',set())['event_type'],'device_event')
        self.assertEqual(normalized_event(event,'gate',{'76'})['event_type'],'device_event')
    def test_explicit_minor_verified(self):
        event={'eventType':'AccessControllerEvent','dateTime':'2026-10-25T10:00:00+05:00',
               'AccessControllerEvent':{'employeeNoString':'STU001','minor':'75'}}
        result=normalized_event(event,'gate',{'75'})
        self.assertEqual(result['person_code'],'STU001')
        self.assertEqual(result['verification_method'],'card')
        self.assertEqual(result['event_type'],'access_granted')
        self.assertEqual(result['event_uid'],normalized_event(event,'gate',{'75'})['event_uid'])
    def test_wrong_event_type_not_verified(self):
        event={'eventType':'VMD','AccessControllerEvent':{'employeeNoString':'STU001','minor':'75'}}
        self.assertEqual(normalized_event(event,'gate',{'75'})['event_type'],'motion')
    def test_payload_cap(self):
        with self.assertRaises(ValueError):decode_event(b'x'*300000)

if __name__=='__main__':unittest.main()
