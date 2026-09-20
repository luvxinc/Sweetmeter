import asyncio
import struct
import tempfile
import unittest
from pathlib import Path
from meter.ota import OTATransfer, UpdateCancelled
from meter.protocol import OTAStatus, OTAState, OTAError, OTA_DATA_UUID, OTA_CONTROL_UUID

class FakeOTA:
    mtu_size = 23
    is_connected = True
    def __init__(self):
        self.callback = None
        self.packets = []
        self.image = bytearray()
        self.envelope = bytearray()
        self.image_size = 100
        self.drop = False
    async def start_notify(self, uuid, callback): self.callback = callback
    async def stop_notify(self, uuid): pass
    async def disconnect(self): self.is_connected = False
    async def read_gatt_char(self, uuid): return self.status.encode()
    def ack(self, state, opcode, offset, total, error=OTAError.OK):
        self.status = OTAStatus(state,error,self.session,offset,total,ord(opcode),1)
        if not self.drop: self.callback(None,self.status.encode())
    async def write_gatt_char(self, uuid, packet, response):
        assert response is True
        self.packets.append((uuid, bytes(packet)))
        if uuid == OTA_DATA_UUID:
            session,offset = struct.unpack_from('<II',packet)
            assert offset == len(self.image)
            self.image.extend(packet[8:])
            self.ack(OTAState.IMAGE,'d',len(self.image),self.image_size)
            return
        opcode=chr(packet[0]); self.session=struct.unpack_from('<I',packet,1)[0]
        if opcode=='M':
            self.envelope_size=struct.unpack_from('<H',packet,5)[0]
            self.ack(OTAState.METADATA,'M',0,self.envelope_size)
        elif opcode=='m':
            offset=struct.unpack_from('<I',packet,5)[0]
            assert offset==len(self.envelope)
            self.envelope.extend(packet[9:])
            self.ack(OTAState.METADATA,'m',len(self.envelope),self.envelope_size)
        elif opcode=='S': self.ack(OTAState.IMAGE,'S',0,self.image_size)
        elif opcode=='F': self.ack(OTAState.REBOOTING,'F',self.image_size,self.image_size)
        elif opcode=='X': self.ack(OTAState.CANCELLED,'X',len(self.image),self.image_size)

class OTATests(unittest.IsolatedAsyncioTestCase):
    async def test_chunk_offsets_beyond_64k_and_small_mtu(self):
        client=FakeOTA(); client.image_size=66000
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'image.bin'; image=b'abcd'*16500; path.write_bytes(image)
            transfer=OTATransfer(client)
            await transfer.run(path,b'e'*180,'2026.9.1',usb_power=True)
        self.assertEqual(bytes(client.image),image)
        self.assertEqual(bytes(client.envelope),b'e'*180)
        self.assertTrue(transfer.commit_started)
        self.assertTrue(all(len(p)<=20 for _,p in client.packets))
    async def test_explicit_usb_confirmation_required_before_writes(self):
        client=FakeOTA(); transfer=OTATransfer(client)
        with self.assertRaises(ValueError): await transfer.run('missing',b'e'*180,'2026.9.1')
        self.assertFalse(client.packets)
    async def test_cancel_aborts_before_commit(self):
        client=FakeOTA()
        transfer=OTATransfer(client,lambda event: transfer.cancel.set())
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'image.bin'; path.write_bytes(b'i'*100)
            with self.assertRaises(UpdateCancelled): await transfer.run(path,b'e'*180,'2026.9.1',usb_power=True)
        self.assertFalse(transfer.commit_started)
        self.assertTrue(any(p[:1]==b'X' for c,p in client.packets if c==OTA_CONTROL_UUID))
    async def test_lost_notification_read_fallback_without_retransmit(self):
        client=FakeOTA(); client.drop=True; transfer=OTATransfer(client)
        await client.start_notify('', transfer.notification)
        packet=struct.pack('<cIHIIIB',b'M',transfer.session,180,2026,9,1,1)
        result=await transfer.exchange(OTA_CONTROL_UUID,packet,'M',OTAState.METADATA,0,180,.01)
        self.assertEqual(result.offset,0)
        self.assertEqual(len(client.packets),1)
    async def test_read_with_unadvanced_offset_fails_without_resend(self):
        client=FakeOTA(); client.drop=True; transfer=OTATransfer(client)
        await client.start_notify('',transfer.notification)
        packet=struct.pack('<cIHIIIB',b'M',transfer.session,180,2026,9,1,1)
        with self.assertRaises(TimeoutError):
            await transfer.exchange(OTA_CONTROL_UUID,packet,'M',OTAState.METADATA,1,180,.01)
        self.assertEqual(len(client.packets),1)
    async def test_foreign_session_cannot_acknowledge(self):
        client=FakeOTA(); transfer=OTATransfer(client)
        transfer.notification(None,OTAStatus(OTAState.METADATA,OTAError.OK,transfer.session+1,0,180,ord('M'),1).encode())
        self.assertTrue(transfer.notifications.empty())
