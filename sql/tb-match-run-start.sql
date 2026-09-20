UPDATE matches SET status = 'running'
 WHERE claim_token = ($1)::uuid AND status = 'claimed'
