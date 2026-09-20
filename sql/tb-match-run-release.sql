UPDATE matches
   SET status = CASE WHEN refusals + 1 >= ($3)::int
                      AND created_at <= now() - ($4)::int * interval '1 second'
                     THEN 'failed' ELSE 'pending' END::match_status,
       refusals = refusals + 1, claim_token = NULL, lease_expires_at = NULL,
       fault_reason = CASE WHEN refusals + 1 >= ($3)::int
                            AND created_at <= now() - ($4)::int * interval '1 second'
                           THEN 'MODEL_UNAVAILABLE' END,
       closed_at = CASE WHEN refusals + 1 >= ($3)::int
                         AND created_at <= now() - ($4)::int * interval '1 second'
                        THEN now() END
 WHERE claim_token = ($1)::uuid AND status = 'claimed' AND ($2)::boolean
