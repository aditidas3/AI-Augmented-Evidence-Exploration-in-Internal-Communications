Qdrant Snapshot Restore
=======================

Snapshot:

```text
align_embeddings-4213334217063050-2026-06-02-03-58-21.snapshot
```

Collection:

```text
align_embeddings
```

Qdrant source version:

```text
1.18.1
```

Snapshot SHA256:

```text
bbb5533c4a960ed15a16b7ae2a3f9092e595efde986b3e1fe960725d5bc131cc
```

Expected point count after restore:

```text
12242
```

Restore into a local Docker Qdrant
----------------------------------

Start Qdrant:

```bash
docker run -d --name qdrant -p 6333:6333 -p 6334:6334 -v qdrant_storage:/qdrant/storage qdrant/qdrant:v1.18.1
```

Copy the snapshot into the container:

```bash
docker exec qdrant mkdir -p /qdrant/snapshots/align_embeddings
docker cp align_embeddings-4213334217063050-2026-06-02-03-58-21.snapshot qdrant:/qdrant/snapshots/align_embeddings/
```

Recover the collection:

```bash
python - <<'PY'
from qdrant_client import QdrantClient
from qdrant_client.http.models import SnapshotPriority

snapshot = "align_embeddings-4213334217063050-2026-06-02-03-58-21.snapshot"
checksum = "bbb5533c4a960ed15a16b7ae2a3f9092e595efde986b3e1fe960725d5bc131cc"

client = QdrantClient(url="http://localhost:6333")
client.recover_snapshot(
    collection_name="align_embeddings",
    location=f"file:///qdrant/snapshots/align_embeddings/{snapshot}",
    checksum=checksum,
    priority=SnapshotPriority.SNAPSHOT,
    wait=True,
)

info = client.get_collection("align_embeddings")
print(f"points_count={info.points_count}")
PY
```

On Windows PowerShell, use this equivalent recovery command:

```powershell
@'
from qdrant_client import QdrantClient
from qdrant_client.http.models import SnapshotPriority

snapshot = "align_embeddings-4213334217063050-2026-06-02-03-58-21.snapshot"
checksum = "bbb5533c4a960ed15a16b7ae2a3f9092e595efde986b3e1fe960725d5bc131cc"

client = QdrantClient(url="http://localhost:6333")
client.recover_snapshot(
    collection_name="align_embeddings",
    location=f"file:///qdrant/snapshots/align_embeddings/{snapshot}",
    checksum=checksum,
    priority=SnapshotPriority.SNAPSHOT,
    wait=True,
)

info = client.get_collection("align_embeddings")
print(f"points_count={info.points_count}")
'@ | python -
```
