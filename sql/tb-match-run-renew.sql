UPDATE matches SET lease_expires_at = now() + ($2)::int * interval '1 second'
 WHERE claim_token = ($1)::uuid AND status = 'running'
