// ============================================================
// Synthetic Enterprise Artifact Constraints
// ============================================================

CREATE CONSTRAINT support_ticket_id_unique IF NOT EXISTS
FOR (t:SupportTicket)
REQUIRE t.ticket_id IS UNIQUE;

CREATE CONSTRAINT logistics_incident_id_unique IF NOT EXISTS
FOR (i:LogisticsIncident)
REQUIRE i.incident_id IS UNIQUE;

CREATE CONSTRAINT customer_email_id_unique IF NOT EXISTS
FOR (e:CustomerEmail)
REQUIRE e.email_id IS UNIQUE;

CREATE CONSTRAINT warranty_claim_id_unique IF NOT EXISTS
FOR (w:WarrantyClaim)
REQUIRE w.claim_id IS UNIQUE;

CREATE CONSTRAINT policy_document_id_unique IF NOT EXISTS
FOR (p:PolicyDocument)
REQUIRE p.document_id IS UNIQUE;

CREATE CONSTRAINT troubleshooting_guide_id_unique IF NOT EXISTS
FOR (g:TroubleshootingGuide)
REQUIRE g.guide_id IS UNIQUE;