"""Make Word and PDF documents from Markdown, and list, download, and delete them (vara/documents.py)."""

import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from vara import store
from vara.db.models import StoredMessage
from vara.documents import MEDIA_TYPES, DocFormat, DocumentStore

documents = APIRouter(prefix="/v1/documents")


def _store(request: Request) -> DocumentStore:
    return request.app.state.documents


class NewDocument(BaseModel):
    markdown: str = Field(min_length=1, max_length=500_000)
    format: DocFormat
    # Without a title, a leading "# Heading" in the Markdown is used.
    title: str | None = Field(default=None, max_length=200)
    # The chat message this is made from, so the file is listed with it when the conversation opens.
    message_id: int | None = None


@documents.post("", status_code=201)
def create(body: NewDocument, request: Request) -> dict:
    saved = _store(request).create(body.markdown, body.format, body.title)
    if body.message_id is not None:
        with request.app.state.db.session() as session:
            if message := session.get(StoredMessage, body.message_id):
                store.attach_document(message, saved.name)
    logging.getLogger("vara").info("Document made: %s (%d bytes)", saved.name, saved.size)
    return saved.to_dict()


@documents.get("")
def list_documents(request: Request) -> list[dict]:
    return _store(request).all()


@documents.get("/{name}")
def download(name: str, request: Request) -> FileResponse:
    path = _store(request).path(name)
    if path is None:
        raise HTTPException(404, "Document not found")
    return FileResponse(path, media_type=MEDIA_TYPES[path.suffix[1:]], filename=name)


@documents.delete("/{name}", status_code=204)
def delete(name: str, request: Request) -> None:
    if not _store(request).delete(name):
        raise HTTPException(404, "Document not found")
