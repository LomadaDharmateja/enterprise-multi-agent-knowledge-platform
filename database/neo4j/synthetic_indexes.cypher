// ============================================================
// Synthetic Enterprise Artifact Indexes
// ============================================================

CREATE INDEX support_ticket_issue_type_index IF NOT EXISTS
FOR (t:SupportTicket)
ON (t.issue_type);

CREATE INDEX support_ticket_severity_index IF NOT EXISTS
FOR (t:SupportTicket)
ON (t.severity);

CREATE INDEX support_ticket_status_index IF NOT EXISTS
FOR (t:SupportTicket)
ON (t.status);

CREATE INDEX logistics_incident_type_index IF NOT EXISTS
FOR (i:LogisticsIncident)
ON (i.incident_type);

CREATE INDEX logistics_incident_severity_index IF NOT EXISTS
FOR (i:LogisticsIncident)
ON (i.severity);

CREATE INDEX warranty_claim_status_index IF NOT EXISTS
FOR (w:WarrantyClaim)
ON (w.claim_status);

CREATE INDEX policy_document_topic_index IF NOT EXISTS
FOR (p:PolicyDocument)
ON (p.policy_topic);

CREATE INDEX troubleshooting_guide_category_index IF NOT EXISTS
FOR (g:TroubleshootingGuide)
ON (g.category_id);