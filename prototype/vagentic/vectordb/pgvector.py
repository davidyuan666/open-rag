# -*- coding = utf-8 -*-
# @time:2024/7/31 10:02
# Author:david yuan
# @File:pgvector.py
# @Software:VeSync

from hashlib import md5
from typing import Optional, List, Union, Dict, Any

from pydantic import BaseModel
from tqdm import tqdm

try:
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.engine import create_engine, Engine
    from sqlalchemy.inspection import inspect
    from sqlalchemy.orm import Session, sessionmaker
    from sqlalchemy.schema import MetaData, Table, Column
    from sqlalchemy.sql.expression import text, func, select
    from sqlalchemy.types import DateTime, String
except ImportError:
    raise ImportError("`sqlalchemy` not installed, please install it via `pip install sqlalchemy`.")

try:
    from pgvector.sqlalchemy import Vector
except ImportError:
    raise ImportError("`pgvector` not installed, please install it via `pip install pgvector`.")

from vagents.vagentic.document import Document
from vagents.vagentic.emb.base import Emb
from vagents.vagentic.vectordb.base import VectorDb, Distance
from vagents.vagentic.emb.openai_emb import OpenAIEmb


class Ivfflat(BaseModel):
    name: Optional[str] = None
    lists: int = 100
    probes: int = 10
    dynamic_lists: bool = True
    configuration: Dict[str, Any] = {
        "maintenance_work_mem": "2GB",
    }


class HNSW(BaseModel):
    name: Optional[str] = None
    m: int = 16
    ef_search: int = 5
    ef_construction: int = 200
    configuration: Dict[str, Any] = {
        "maintenance_work_mem": "2GB",
    }


class PgVector(VectorDb):
    def __init__(
            self,
            collection: str,
            schema: Optional[str] = "ai",
            db_url: Optional[str] = None,
            db_engine: Optional[Engine] = None,
            embedder: Optional[Emb] = None,
            distance: Distance = Distance.cosine,
            index: Optional[Union[Ivfflat, HNSW]] = HNSW(),
    ):
        _engine: Optional[Engine] = db_engine
        if _engine is None and db_url is not None:
            _engine = create_engine(db_url)

        if _engine is None:
            raise ValueError("Must provide either db_url or db_engine")

        # Collection attributes
        self.collection: str = collection
        self.schema: Optional[str] = schema

        # Database attributes
        self.db_url: Optional[str] = db_url
        self.db_engine: Engine = _engine
        self.metadata: MetaData = MetaData(schema=self.schema)

        # Emb for embedding the document contents
        _embedder = embedder
        if _embedder is None:
            _embedder = OpenAIEmb()
        self.embedder: embedder = _embedder
        self.dimensions: int = self.embedder.dimensions

        # Distance metric
        self.distance: Distance = distance

        # Index for the collection
        self.index: Optional[Union[Ivfflat, HNSW]] = index

        # Database session
        self.Session: sessionmaker[Session] = sessionmaker(bind=self.db_engine)

        # Database table for the collection
        self.table: Table = self.get_table()

    def get_table(self) -> Table:
        return Table(
            self.collection,
            self.metadata,
            Column("id", String, primary_key=True),
            Column("name", String),
            Column("meta_data", postgresql.JSONB, server_default=text("'{}'::jsonb")),
            Column("content", postgresql.TEXT),
            Column("embedding", Vector(self.dimensions)),
            Column("usage", postgresql.JSONB),
            Column("created_at", DateTime(timezone=True), server_default=text("now()")),
            Column("updated_at", DateTime(timezone=True), onupdate=text("now()")),
            Column("content_hash", String),
            extend_existing=True,
        )

    def table_exists(self) -> bool:
        try:
            return inspect(self.db_engine).has_table(self.table.name, schema=self.schema)
        except Exception as e:
            logger.error(e)
            return False

    def create(self) -> None:
        if not self.table_exists():
            with self.Session() as sess:
                with sess.begin():
                    sess.execute(text("create extension if not exists vector;"))
                    if self.schema is not None:
                        sess.execute(text(f"create schema if not exists {self.schema};"))
            self.table.create(self.db_engine)

    def doc_exists(self, document: Document) -> bool:
        """
        Validating if the document exists or not

        Args:
            document (Document): Document to validate
        """
        columns = [self.table.c.name, self.table.c.content_hash]
        with self.Session() as sess:
            with sess.begin():
                cleaned_content = document.content.replace("\x00", "\ufffd")
                stmt = select(*columns).where(self.table.c.content_hash == md5(
                    cleaned_content.encode()).hexdigest())
                result = sess.execute(stmt).first()
                return result is not None

    def name_exists(self, name: str) -> bool:
        """
        Validate if a row with this name exists or not

        Args:
            name (str): Name to check
        """
        with self.Session() as sess:
            with sess.begin():
                stmt = select(self.table.c.name).where(self.table.c.name == name)
                result = sess.execute(stmt).first()
                return result is not None

    def id_exists(self, id: str) -> bool:
        """
        Validate if a row with this id exists or not

        Args:
            id (str): Id to check
        """
        with self.Session() as sess:
            with sess.begin():
                stmt = select(self.table.c.id).where(self.table.c.id == id)
                result = sess.execute(stmt).first()
                return result is not None

    def insert(self, documents: List[Document], batch_size: int = 10) -> None:
        with self.Session() as sess:
            counter = 0
            for document in tqdm(documents, desc="Inserting documents"):
                document.embed(embedder=self.embedder)
                cleaned_content = document.content.replace("\x00", "\ufffd")
                content_hash = md5(cleaned_content.encode()).hexdigest()
                _id = document.id or content_hash
                stmt = postgresql.insert(self.table).values(
                    id=_id,
                    name=document.name,
                    meta_data=document.meta_data,
                    content=cleaned_content,
                    embedding=document.embedding,
                    usage=document.usage,
                    content_hash=content_hash,
                )
                sess.execute(stmt)
                counter += 1

                # Commit every `batch_size` documents
                if counter >= batch_size:
                    sess.commit()
                    counter = 0

            # Commit any remaining documents
            if counter > 0:
                sess.commit()

    def upsert_available(self) -> bool:
        return True

    def upsert(self, documents: List[Document], batch_size: int = 20) -> None:
        """
        Upsert documents into the database.

        Args:
            documents (List[Document]): List of documents to upsert
            batch_size (int): Batch size for upserting documents
        """
        with self.Session() as sess:
            counter = 0
            for document in documents:
                document.embed(embedder=self.embedder)
                cleaned_content = document.content.replace("\x00", "\ufffd")
                content_hash = md5(cleaned_content.encode()).hexdigest()
                _id = document.id or content_hash
                stmt = postgresql.insert(self.table).values(
                    id=_id,
                    name=document.name,
                    meta_data=document.meta_data,
                    content=cleaned_content,
                    embedding=document.embedding,
                    usage=document.usage,
                    content_hash=content_hash,
                )
                # Update row when id matches but 'content_hash' is different
                stmt = stmt.on_conflict_do_update(
                    index_elements=["id"],
                    set_=dict(
                        name=stmt.excluded.name,
                        meta_data=stmt.excluded.meta_data,
                        content=stmt.excluded.content,
                        embedding=stmt.excluded.embedding,
                        usage=stmt.excluded.usage,
                        content_hash=stmt.excluded.content_hash,
                    ),
                )
                sess.execute(stmt)
                counter += 1

                # Commit every `batch_size` documents
                if counter >= batch_size:
                    sess.commit()
                    counter = 0

            # Commit any remaining documents
            if counter > 0:
                sess.commit()

    def search(self, query: str, limit: int = 5, filters: Optional[Dict[str, Any]] = None) -> List[Document]:
        query_embedding = self.embedder.get_embedding(query)
        if query_embedding is None:
            return []

        columns = [
            self.table.c.name,
            self.table.c.meta_data,
            self.table.c.content,
            self.table.c.embedding,
            self.table.c.usage,
        ]

        stmt = select(*columns)

        if filters is not None:
            for key, value in filters.items():
                if hasattr(self.table.c, key):
                    stmt = stmt.where(getattr(self.table.c, key) == value)

        if self.distance == Distance.l2:
            stmt = stmt.order_by(self.table.c.embedding.max_inner_product(query_embedding))
        if self.distance == Distance.cosine:
            stmt = stmt.order_by(self.table.c.embedding.cosine_distance(query_embedding))
        if self.distance == Distance.max_inner_product:
            stmt = stmt.order_by(self.table.c.embedding.max_inner_product(query_embedding))

        stmt = stmt.limit(limit=limit)

        # Get neighbors
        try:
            with self.Session() as sess:
                with sess.begin():
                    if self.index is not None:
                        if isinstance(self.index, Ivfflat):
                            sess.execute(text(f"SET LOCAL ivfflat.probes = {self.index.probes}"))
                        elif isinstance(self.index, HNSW):
                            sess.execute(text(f"SET LOCAL hnsw.ef_search  = {self.index.ef_search}"))
                    neighbors = sess.execute(stmt).fetchall() or []
        except Exception as e:
            self.create()
            return []

        # Build search results
        search_results: List[Document] = []
        for neighbor in neighbors:
            search_results.append(
                Document(
                    name=neighbor.name,
                    meta_data=neighbor.meta_data,
                    content=neighbor.content,
                    embedder=self.embedder,
                    embedding=neighbor.embedding,
                    usage=neighbor.usage,
                )
            )

        return search_results

    def delete(self) -> None:
        if self.table_exists():
            self.table.drop(self.db_engine)

    def exists(self) -> bool:
        return self.table_exists()

    def get_count(self) -> int:
        with self.Session() as sess:
            with sess.begin():
                stmt = select(func.count(self.table.c.name)).select_from(self.table)
                result = sess.execute(stmt).scalar()
                if result is not None:
                    return int(result)
                return 0

    def optimize(self) -> None:
        from math import sqrt

        if self.index is None:
            return

        if self.index.name is None:
            _type = "ivfflat" if isinstance(self.index, Ivfflat) else "hnsw"
            self.index.name = f"{self.collection}_{_type}_index"

        index_distance = "vector_cosine_ops"
        if self.distance == Distance.l2:
            index_distance = "vector_l2_ops"
        if self.distance == Distance.max_inner_product:
            index_distance = "vector_ip_ops"

        if isinstance(self.index, Ivfflat):
            num_lists = self.index.lists
            if self.index.dynamic_lists:
                total_records = self.get_count()
                if total_records < 1000000:
                    num_lists = int(total_records / 1000)
                elif total_records > 1000000:
                    num_lists = int(sqrt(total_records))

            with self.Session() as sess:
                with sess.begin():
                    for key, value in self.index.configuration.items():
                        sess.execute(text(f"SET {key} = '{value}';"))
                    sess.execute(text(f"SET ivfflat.probes = {self.index.probes};"))
                    sess.execute(
                        text(
                            f"CREATE INDEX IF NOT EXISTS {self.index.name} ON {self.table} "
                            f"USING ivfflat (embedding {index_distance}) "
                            f"WITH (lists = {num_lists});"
                        )
                    )
        elif isinstance(self.index, HNSW):
            with self.Session() as sess:
                with sess.begin():
                    for key, value in self.index.configuration.items():
                        sess.execute(text(f"SET {key} = '{value}';"))
                    sess.execute(
                        text(
                            f"CREATE INDEX IF NOT EXISTS {self.index.name} ON {self.table} "
                            f"USING hnsw (embedding {index_distance}) "
                            f"WITH (m = {self.index.m}, ef_construction = {self.index.ef_construction});"
                        )
                    )

    def clear(self) -> bool:
        from sqlalchemy import delete

        with self.Session() as sess:
            with sess.begin():
                stmt = delete(self.table)
                sess.execute(stmt)
                return True
