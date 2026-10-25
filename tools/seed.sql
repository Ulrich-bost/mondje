INSERT INTO transactions (id, type, amount, description, agency, created_at)
SELECT gen_random_uuid(),
       CASE WHEN random() < 0.5 THEN 'CREDIT' ELSE 'DEBIT' END,
       1 + floor(random() * 500000)::bigint,
       (ARRAY['cacao','cafe','engrais','semences','salaire','loyer','transport','vente','achat','credit'])[1 + floor(random() * 10)::int]
         || ' ' ||
       (ARRAY['douala','yaounde','bafoussam','marche','cooperative','recolte','facture','avance','remboursement','stock'])[1 + floor(random() * 10)::int]
         || ' ' || g,
       (ARRAY['douala','yaounde','bafoussam'])[1 + floor(random() * 3)::int],
       now() - random() * interval '365 days'
FROM generate_series(1, 1000000) AS g;
VACUUM ANALYZE transactions;
ANALYZE daily_balance;
