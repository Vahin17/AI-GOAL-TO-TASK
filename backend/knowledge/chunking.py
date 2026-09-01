"""
chunking.py — Document Chunking & Deduplication
=================================================
Implements recursive character chunking with configurable overlap,
content hashing, token estimation, and duplicate filtering.
"""

from __future__ import annotations

import hashlib
import re
from typing import List, Set, Tuple

from knowledge.knowledge_schemas import Document, DocumentChunk


class TextChunker:
    """
    Splits Document instances into cleanly bounded DocumentChunk objects.
    """

    def __init__(
        self,
        chunk_size: int = 500,
        chunk_overlap: int = 60,
        min_chunk_length: int = 40,
    ):
        if chunk_overlap >= chunk_size:
            raise ValueError(f"chunk_overlap ({chunk_overlap}) must be smaller than chunk_size ({chunk_size})")
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.min_chunk_length = min_chunk_length

    def chunk_document(self, doc: Document) -> List[DocumentChunk]:
        """
        Decomposes a single Document into a list of DocumentChunks with metadata.
        """
        raw_text = doc.content.strip()
        if not raw_text:
            return []

        text_splits = self._recursive_split(raw_text, self.chunk_size, self.chunk_overlap)
        
        chunks: List[DocumentChunk] = []
        seen_hashes: Set[str] = set()

        for idx, text in enumerate(text_splits):
            cleaned = text.strip()
            if len(cleaned) < self.min_chunk_length:
                continue

            content_hash = hashlib.sha256(cleaned.encode("utf-8")).hexdigest()[:16]
            if content_hash in seen_hashes:
                continue
            seen_hashes.add(content_hash)

            chunk_meta = {
                **doc.metadata,
                "title": doc.title,
                "category": doc.category,
                "source": doc.source,
                "chunk_index": idx,
            }

            chunks.append(
                DocumentChunk(
                    document_id=doc.document_id,
                    source=doc.source,
                    chunk_index=idx,
                    content=cleaned,
                    char_count=len(cleaned),
                    token_estimate=max(1, len(cleaned) // 4),
                    content_hash=content_hash,
                    metadata=chunk_meta,
                )
            )

        return chunks

    def chunk_documents(self, docs: List[Document]) -> Tuple[List[DocumentChunk], int]:
        """
        Chunks multiple documents. Returns (chunks, duplicates_skipped_count).
        """
        all_chunks: List[DocumentChunk] = []
        seen_global_hashes: Set[str] = set()
        duplicates_skipped = 0

        for doc in docs:
            doc_chunks = self.chunk_document(doc)
            for c in doc_chunks:
                if c.content_hash in seen_global_hashes:
                    duplicates_skipped += 1
                    continue
                seen_global_hashes.add(c.content_hash)
                all_chunks.append(c)

        return all_chunks, duplicates_skipped

    def _recursive_split(self, text: str, chunk_size: int, overlap: int) -> List[str]:
        """
        Splits text by hierarchical delimiters: \\n\\n -> \\n -> . -> space -> char.
        """
        if len(text) <= chunk_size:
            return [text]

        separators = ["\n\n", "\n", ". ", "; ", ", ", " "]
        return self._split_text_with_separators(text, separators, chunk_size, overlap)

    def _split_text_with_separators(
        self,
        text: str,
        separators: List[str],
        chunk_size: int,
        overlap: int,
    ) -> List[str]:
        final_chunks: List[str] = []
        
        # Pick first separator that exists in text
        separator = ""
        for s in separators:
            if s in text:
                separator = s
                break

        if not separator:
            # Fallback to hard character slicing
            step = chunk_size - overlap
            return [text[i:i + chunk_size] for i in range(0, len(text), max(1, step))]

        splits = text.split(separator)
        current_chunk: List[str] = []
        current_len = 0

        for segment in splits:
            seg_len = len(segment)
            if current_len + seg_len + len(separator) > chunk_size and current_chunk:
                merged = separator.join(current_chunk)
                final_chunks.append(merged)
                
                # Keep overlap items from the tail
                overlap_accum: List[str] = []
                accum_len = 0
                for item in reversed(current_chunk):
                    if accum_len + len(item) + len(separator) <= overlap:
                        overlap_accum.insert(0, item)
                        accum_len += len(item) + len(separator)
                    else:
                        break
                current_chunk = overlap_accum
                current_len = accum_len

            current_chunk.append(segment)
            current_len += seg_len + len(separator)

        if current_chunk:
            final_chunks.append(separator.join(current_chunk))

        return final_chunks
