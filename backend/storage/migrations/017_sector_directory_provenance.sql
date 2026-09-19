-- Keep directory fallback evidence with the same atomic market snapshot.
ALTER TABLE sector_heat_snapshot ADD COLUMN directory_provenance_json TEXT;
