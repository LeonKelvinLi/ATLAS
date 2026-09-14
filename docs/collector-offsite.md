# Private Google Cloud Storage backups

This optional extension sends a completed, locally verified ATLAS snapshot to a
private Cloud Storage bucket, downloads its exact object generation, and verifies
the downloaded snapshot. It does not upload the live state tree, load broker
credentials, deserialize model pickles, or enable trading. Installation and
runtime verification are separate checkpoints; deployment receipts belong in
the external hosting evidence directory.

The local daily backup remains restricted to Unix sockets. Its optional
`OnSuccess` hook starts a separate uploader service. A shared backup-root lock
serializes snapshot creation/pruning with source selection and packaging. Once
the upload package is complete, collection and local backup retention can
continue independently of remote transfer. Failed upload or verification must
not be recorded as a verified offsite backup.

## Fixed infrastructure

| Item | Selection |
|---|---|
| Owner account | `leondragon3798@gmail.com`, selected explicitly for ATLAS |
| Project | `atlas-collector-20260914` |
| Bucket | `atlas-collector-backups-836932812958` |
| Location/class | Regional `us-central1`, `STANDARD` |
| Identity | `atlas-backup-writer@atlas-collector-20260914.iam.gserviceaccount.com` |
| Custom role | `projects/atlas-collector-20260914/roles/atlasBackupObjectCreateRead` |
| Permissions | `storage.objects.create`, `storage.objects.get` only |
| Binding scope | This bucket, restricted to object prefix `backups/` |
| Access controls | Uniform bucket-level access; public access prevention enforced |
| Lifecycle | Delete `backups/` objects after seven days; seven-day soft deletion |
| Other features | Versioning and Autoclass disabled; no locked bucket retention policy |

The uploader cannot list, delete, replace, restore soft-deleted objects, modify
bucket policy, or administer the VM through this role. Creating an existing
object requires delete permission as well as create permission; upload requests
also use a create-only generation precondition. Owner recovery access remains
separate. Do not replace the bucket's entire IAM policy when adding this binding.
[Storage permissions](https://docs.cloud.google.com/storage/docs/access-control/iam-permissions),
[conditional access](https://docs.cloud.google.com/storage/docs/access-control/iam#conditions).

No service-account private key is generated or copied. The VM obtains short-lived
credentials through its attached service account. Changing from the current
no-service-account configuration requires a controlled VM stop/start. Bucket,
role, source code and systemd preparation can be completed before that stop.
Record the resulting collection discontinuity and start uptime measurement after
final activation. Any process able to use the VM's metadata credentials receives
the same narrow bucket permissions; this is not per-process IAM isolation.
[Change the attached service account](https://docs.cloud.google.com/compute/docs/instances/change-service-account).

## Provisioning commands

These commands describe the selected ATLAS resources. Inspect existing resources
first and reconcile differences; do not blindly recreate or overwrite them.
They preserve the user's default gcloud account/project because every command
selects the ATLAS account and project explicitly. Run from the repository root.

```sh
gcloud iam service-accounts create atlas-backup-writer \
  --display-name='ATLAS verified backup writer' \
  --account=leondragon3798@gmail.com --project=atlas-collector-20260914

gcloud iam roles create atlasBackupObjectCreateRead \
  --file=deploy/gcs-offsite-role.json \
  --account=leondragon3798@gmail.com --project=atlas-collector-20260914

gcloud storage buckets create gs://atlas-collector-backups-836932812958 \
  --location=us-central1 --default-storage-class=STANDARD \
  --uniform-bucket-level-access --public-access-prevention \
  --no-enable-autoclass --soft-delete-duration=7d \
  --lifecycle-file=deploy/gcs-offsite-lifecycle.json \
  --account=leondragon3798@gmail.com --project=atlas-collector-20260914

gcloud storage buckets update gs://atlas-collector-backups-836932812958 \
  --no-versioning \
  --account=leondragon3798@gmail.com --project=atlas-collector-20260914

gcloud storage buckets add-iam-policy-binding gs://atlas-collector-backups-836932812958 \
  --member=serviceAccount:atlas-backup-writer@atlas-collector-20260914.iam.gserviceaccount.com \
  --role=projects/atlas-collector-20260914/roles/atlasBackupObjectCreateRead \
  --condition-from-file=deploy/gcs-offsite-condition.json \
  --account=leondragon3798@gmail.com --project=atlas-collector-20260914

gcloud compute instances stop atlas-collector-1 --zone=us-central1-b \
  --account=leondragon3798@gmail.com --project=atlas-collector-20260914

gcloud compute instances set-service-account atlas-collector-1 --zone=us-central1-b \
  --service-account=atlas-backup-writer@atlas-collector-20260914.iam.gserviceaccount.com \
  --scopes=storage-rw \
  --account=leondragon3798@gmail.com --project=atlas-collector-20260914

gcloud compute instances start atlas-collector-1 --zone=us-central1-b \
  --account=leondragon3798@gmail.com --project=atlas-collector-20260914
```

Read back the bucket settings, custom role, conditional bucket binding and VM
service-account/scopes before calling the uploader. Access scopes are an
additional ceiling; the bucket IAM binding supplies the narrow resource rights.
Do not grant Storage Admin, Object Admin, Editor, or Owner to the uploader.

## Install and verify

The release allowlist adds only `atlas/collection/offsite.py` and
`scripts/atlas_offsite_backup.py`. The installer verifies both the complete
pre-offsite release allowlist and the complete current allowlist so an existing
verified installation can be upgraded and rolled back. Arbitrary partial release
manifests are refused. An update still backs up existing data before switching
code; it does not replace the registered study or restore older data.

On the selected VM, after transferring and checking the complete release bundle:

```sh
sudo sh ./deploy.sh --offsite-bucket atlas-collector-backups-836932812958
sudo systemctl start atlas-collector-backup.service
sudo systemctl status atlas-collector-offsite.service --no-pager
sudo journalctl -u atlas-collector-offsite.service -n 20 --no-pager
```

`--offsite-bucket` writes a nonsecret bucket-only environment file and installs
`atlas-collector-backup.service.d/20-offsite.conf`. The existing timer remains
04:15 UTC daily with up to five minutes of jitter. A successful local backup
triggers the uploader. The uploader has no independent timer and no automatic
permission to alter retention or cloud policy. Enabling the hook does not by
itself prove an upload, readback, or restore succeeded.

The uploader runs as `atlas-collector`, with private staging and its sanitized
receipt at `/var/lib/atlas-collector-offsite/latest.json`. Its service namespace
hides the live state directory and the installed Alpaca key. The backup-root
directory is writable only because the shared coordination lock lives there;
the uploader implementation has no pruning operation. The bucket name is passed
through `/etc/atlas-collector/offsite.env`; no bearer token or private key belongs
in that file, the journal, or the backup.

Verify the receipt's exact bucket/object/generation, checksum equality and
downloaded snapshot integrity. Retrying the same backup must read back the same
object or refuse a conflict, without overwriting it. Also perform one separate
restore from a downloaded remote snapshot to a new private directory, then check
the original 14,843-record historical archive and frozen artifacts. Daily
downloaded-snapshot verification is explicitly distinct from that restore drill;
it does not create an unnecessary second restored payload on the small VM.
[Cloud Storage checksum validation](https://docs.cloud.google.com/storage/docs/data-validation).

The initial manual Mac copy/restore remains useful evidence, but it is not proof
of the new scheduled GCS path. Require an actual successful scheduled run as well
as the initial manual invocation. Uploader failure remains visible in systemd
and its receipt; a local backup success cannot substitute for remote success.

## Local retention and capacity gate

Keep the existing seven-local-snapshot policy during initial activation. Only
after remote upload/readback verification and the separate restore proof pass,
install the prepared two-snapshot override:

```sh
sudo install -m 0644 deploy/atlas-collector-backup-retention-2.conf \
  /etc/systemd/system/atlas-collector-backup.service.d/50-retention.conf
sudo systemctl daemon-reload
sudo systemctl cat atlas-collector-backup.service
```

This change affects subsequent verified backup creation. It does not manually
remove existing snapshots, failed snapshots, archives, or research evidence. The
backup implementation prunes only verified managed snapshots after creating a
new verified snapshot. Preserve the retention override across future upgrades.

Two retained local copies are the initial choice for the 30 GB disk. If the live
archive occupies `D` bytes, steady state is roughly `3D` before OS/logs, and
creating the next snapshot can temporarily reach `4D`. The uploader reserves
space for its uncompressed verified payload and worst-case compressed package,
without assuming compression savings; the selected service additionally keeps a
1 GiB free-space reserve. A downloaded package and extraction can add roughly
`2D`, so the complete cycle can approach `5D` plus OS/logs. At the earlier 5.7 GB
archive projection this is not proven to fit 30 GB. A full restore drill needs
its own explicit workspace budget or a different machine.

Measure actual source, snapshots, staging peaks and free space daily. A space
refusal is a capacity blocker: retain evidence, report the failure and choose
verified compression/retention changes or a priced disk expansion before growth
reaches the limit. Do not erase data or reduce the reserve to force success.

## Cost and cloud retention

Regional Standard storage in `us-central1` is approximately $0.02/GiB-month.
Eligible account-wide free usage includes 5 GB-months, 5,000 Class A operations,
and 50,000 Class B operations. Same-region GCS readback has no data-transfer fee.
At a conservative 80 GiB retained, storage is roughly $1.50/month after available
free allowance, placing IPv4 plus storage near $5.10–$5.22/month before other
usage. This is a projection, not a bill or a hard spending cap; unused free
allowance, operations, local disk expansion and actual growth must be checked.
[Storage pricing](https://cloud.google.com/storage/pricing).

Seven-day lifecycle deletion plus seven-day soft deletion means approximately
fourteen daily copies remain billable. Lifecycle deletion is asynchronous, and
an age rule does not count successful restore points. If uploads stop, old cloud
objects still age out; use backup-age monitoring and treat repeated failures as
actionable. The uploader itself cannot delete objects or change this policy.
Do not lock the bucket retention policy as part of this setup.
[Lifecycle behavior](https://docs.cloud.google.com/storage/docs/lifecycle),
[soft delete](https://docs.cloud.google.com/storage/docs/soft-delete).
