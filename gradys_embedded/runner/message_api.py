from fastapi import FastAPI
from pydantic import BaseModel

from gradys_embedded.encapsulator.interface import IEncapsulator


class MessagePayload(BaseModel):
    message: str
    source: int


def create_message_app(encapsulator: IEncapsulator) -> FastAPI:
    app = FastAPI()

    @app.post("/message")
    async def receive_message(payload: MessagePayload):
        encapsulator.handle_packet(payload.message)
        return {"status": "ok"}

    return app
