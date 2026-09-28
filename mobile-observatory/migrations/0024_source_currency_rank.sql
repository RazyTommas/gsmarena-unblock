BEGIN;

-- Which publisher to believe about what a device is running NOW.
--
-- Promoting the Transsion products linked firmware from several publishers onto
-- the same canonical devices, and 36 (device, region, channel) partitions ended
-- up drawing on two sources at once. The projection's validator refused to
-- publish, correctly: its ranking breaks ties by date, and comparing a date
-- from one publisher against a date from another is the exact thing this corpus
-- is not allowed to do.
--
-- authority_scope cannot settle it. Both sides of every one of those 36
-- partitions -- frbox.community.transsion_catalog and google.ota.checkin -- are
-- 'secondary', which is true and useless here. The distinction that matters for
-- "what is it running now" is not how authoritative a source is in general, it
-- is whether the source describes CURRENT DISTRIBUTION or HISTORY:
--
--   google.ota.checkin is Google's update servers answering, right now, what
--   they would hand this device. It cannot describe a build that is no longer
--   offered.
--   frbox / naijarom / mifirm are archives. They are broader and older, and a
--   row in them is evidence a build existed, not that it is current.
--
-- So the ordering is stored as data on the source rather than written into a
-- CASE in the query: it is a judgement, it should be visible, reviewable and
-- changeable without a code deploy, and a new source must be given a rank
-- deliberately rather than inheriting one from where it happens to sort.
--
-- Lower rank wins. The default of 50 is NOT inert: a new source arrives already
-- ranked alongside the community archives, so it can win a partition of its own
-- immediately, and if it shares a partition with another rank-50 publisher the
-- build refuses to publish until someone separates them. That refusal is the
-- intended behaviour -- an unranked publisher is one nobody has decided about,
-- and guessing between two of them is exactly what this column exists to
-- prevent -- but it is a refusal, not a no-op, and adding a source to a corpus
-- that already has community data may require ranking it in the same change.
ALTER TABLE sources ADD COLUMN currency_rank INTEGER NOT NULL DEFAULT 50;

-- Vendor/platform distribution endpoints: these answer "what would you send
-- this device today".
UPDATE sources SET currency_rank = 10
 WHERE id IN ('google.ota.checkin', 'google.ota.checkin.tecno', 'samsung.fota');

-- Vendor documentation: authoritative about a build, published on the vendor's
-- own schedule rather than on request.
UPDATE sources SET currency_rank = 20
 WHERE id IN ('samsung.doc.aspl', 'tecno.vendor.security_device_scope');

-- Community archives: broad and historical. A row here is evidence a build
-- existed, not that it is the one being shipped.
UPDATE sources SET currency_rank = 50
 WHERE id IN ('frbox.community.transsion_catalog', 'naijarom.community.transsion_firmware',
              'mifirm.community.firmware_archive', 'xiaomi.community.firmware_tracker',
              'ipsw.me.firmware_index');

INSERT INTO schema_migrations(version,name,applied_at)
VALUES(24,'source_currency_rank',strftime('%Y-%m-%dT%H:%M:%fZ','now'));
COMMIT;
