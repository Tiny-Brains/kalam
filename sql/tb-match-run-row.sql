SELECT json_build_object(
         'id', m.id, 'seed', m.seed, 'map_id', sm.map_id, 'map', sm.board, 'seat_count', m.seat_count,
         'trial_model_id', m.trial_version_id, 'strike_ceiling', m.strike_ceiling,
         'seats', (SELECT json_agg(json_build_object(
                     'm', 0, 'seat', s.seat, 'version_id', s.version_id,
                     'model', ($2)::text || s.version_id::text,
                     'strike_ceiling', m.strike_ceiling,
                     'weights_hash', s.weights_hash,
                     'manifest_hash', s.manifest_hash) ORDER BY s.seat)
                    FROM match_seats s WHERE s.match_id = m.id)) AS row
  FROM matches m
  JOIN season_maps sm ON sm.id = m.season_map_id
 WHERE m.claim_token = ($1)::uuid AND m.status = 'claimed'
