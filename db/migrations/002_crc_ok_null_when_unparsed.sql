-- `crc_ok` is now null when the document couldn't be parsed: it isn't EVA-DTS
-- at all (an HTML error page, a truncated body), or a field isn't a number. So
-- `false` means only that the checksum was computed and failed.
--
-- Rows stored before this migration can't be told apart, so they keep `false`.

alter table polls alter column crc_ok drop not null;
