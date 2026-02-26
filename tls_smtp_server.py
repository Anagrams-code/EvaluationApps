import ssl
import asyncio
from aiosmtpd.controller import Controller
from aiosmtpd.handlers import Message

class BasicHandler(Message):
    async def handle_DATA(self, server, session, envelope):
        print("=== Received message ===")
        print(envelope.content.decode('utf-8', errors='replace'))
        print("=== End message ===")
        return '250 Message accepted for delivery'

ssl_ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
ssl_ctx.load_cert_chain(certfile='cert.pem', keyfile='certkey.pem')

controller = Controller(BasicHandler(), hostname='localhost', port=1025, ssl_context=ssl_ctx, authenticator=None)

print('Starting TLS-capable SMTP server on localhost:1025')
controller.start()
try:
    asyncio.get_event_loop().run_forever()
except KeyboardInterrupt:
    controller.stop()
