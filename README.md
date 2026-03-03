# Intelligent Active Directory User Onboarding System

## Project Overview
Build an automated employee onboarding system that uses AI to process natural language requests and provision Active Directory accounts with appropriate permissions based on role and department.

**Time Estimate:** 2-3 days  
**Difficulty:** Intermediate  
**Cost:** ~$5-10 (AWS Free Tier eligible)

---

## Technical Architecture

### Components
1. **AWS Lambda** - Serverless function to orchestrate the workflow
2. **LangChain** - AI agent to interpret onboarding requests and determine appropriate AD groups
3. **Active Directory** - User and group management (can use AWS Directory Service or on-prem AD)
4. **API Gateway** - REST endpoint to receive onboarding requests
5. **DynamoDB** - Store onboarding requests and audit logs
6. **SNS** - Send notifications to IT team and hiring managers

### Data Flow
```
Slack/Teams → API Gateway → Lambda → LangChain (analyzes request) 
    → Lambda (creates AD user) → DynamoDB (logs) → SNS (notifies)
```

---

## Technical Implementation

### Phase 1: Infrastructure Setup (Decision Point)
**Decision:** Use AWS Managed Microsoft AD vs. EC2-based Domain Controller

**Your Choice:** AWS Managed Microsoft AD
- **Reasoning:** Higher availability (99.9% SLA), automated patching, simplified management
- **Trade-off:** Higher cost (~$100/month) but acceptable for enterprise use
- **Alternative considered:** EC2 + Windows Server AD (cheaper but requires more maintenance)

### Phase 2: Lambda Function Development

**Key Technical Decisions:**

1. **Runtime:** Python 3.11
   - Native boto3 support for AWS services
   - Excellent LangChain integration
   - Familiar to most engineers

2. **LangChain Setup:**
   - Use Claude via Bedrock (stays within AWS ecosystem)
   - Implement prompt template for role-based group assignment
   - Few-shot learning with example role mappings

3. **AD Integration:**
   - Use `ldap3` library for Python
   - Implement secure credential management via AWS Secrets Manager
   - Create service account with limited permissions (principle of least privilege)

### Phase 3: AI Logic Implementation

**LangChain Prompt Engineering:**
```python
# Example structure (not full code)
- Parse employee details: name, role, department, manager
- Analyze role requirements using few-shot examples
- Map to AD security groups: base access + role-specific groups
- Generate user attributes: email, display name, organizational unit
- Validate group assignments before execution
```

**Technical Challenge Solved:** Handling ambiguous role descriptions
- **Solution:** Implement confidence scoring; if < 80% confidence, flag for manual review
- **Demonstrates:** Error handling and human-in-the-loop design

### Phase 4: Security & Compliance

**Implementations:**
1. Input validation to prevent LDAP injection
2. Encrypted environment variables for AD credentials
3. CloudWatch Logs for full audit trail
4. IAM roles with minimal required permissions
5. DynamoDB streams for compliance reporting

---

## Non-Technical Stakeholder Summary

### Problem Statement
Currently, IT teams spend 45-60 minutes manually creating each new employee account, determining correct permissions, and coordinating across systems. This delays employee productivity and ties up IT resources.

### Solution
An intelligent system that automatically creates employee accounts and assigns permissions based on their role, reducing onboarding time from hours to minutes.

### Business Benefits
- **Time Savings:** Reduce onboarding from 60 minutes to 5 minutes (92% reduction)
- **Cost Efficiency:** $50/onboarding → $2/onboarding in labor costs
- **Consistency:** Eliminate human error in permission assignment
- **Scalability:** Handle 100+ onboardings per day without additional staff
- **Audit Trail:** Complete record of all account creations for compliance

### How It Works (Simple Terms)
1. HR submits new employee info via Slack/Teams
2. AI reads the job title and department, determines what system access they need
3. System automatically creates their account with correct permissions
4. IT team receives confirmation notification
5. Employee can log in on Day 1

### ROI Calculation
- Initial build: 16-24 hours ($2,000-3,000 in engineering time)
- Monthly AWS costs: ~$50-100
- Savings: 55 minutes × $50/hour × 20 employees/month = $916/month
- **Payback period:** 3-4 months

---

## Demonstration Materials

### What to Include in Portfolio

1. **Architecture Diagram** (draw.io or Lucidchart)
   - Show all components and data flow
   - Highlight security boundaries

2. **GitHub Repository** with:
   - Well-commented Lambda code
   - Infrastructure as Code (Terraform or CloudFormation)
   - README with setup instructions
   - Sample API requests/responses

3. **Demo Video** (5-7 minutes):
   - Show onboarding request submission
   - Walk through Lambda logs showing LangChain reasoning
   - Display AD user creation in Active Directory Users & Computers
   - Show audit log in DynamoDB

4. **Technical Write-Up** covering:
   - Decision points and reasoning
   - Challenges encountered and solutions
   - Security considerations
   - Future enhancements

5. **Business One-Pager**:
   - Problem, solution, benefits (for non-technical viewers)
   - Use the stakeholder summary above

### Key Talking Points for Interviews

- **Why LangChain?** "Needed intelligent role interpretation without building a complex rules engine"
- **Why Lambda?** "Event-driven architecture; only pay when onboarding occurs"
- **Security?** "Least privilege IAM, encrypted credentials, full audit trail, input validation"
- **Scalability?** "Serverless automatically handles concurrent requests; tested up to 50 simultaneous onboardings"

---

## Extensions (Optional)

To make this project even more impressive:

1. **Multi-cloud AD sync** - Sync to Azure AD for Microsoft 365 access
2. **Approval workflow** - Add Step Functions for manager approval before execution
3. **Machine learning** - Train model on historical access patterns to suggest groups
4. **Offboarding** - Reverse process that disables accounts and logs activity
5. **Self-service portal** - React frontend for HR to submit requests

---

## Sample Code Structure

```
/ad-onboarding-automation
├── /lambda
│   ├── handler.py (main Lambda function)
│   ├── ad_manager.py (AD operations)
│   ├── langchain_agent.py (AI logic)
│   └── requirements.txt
├── /infrastructure
│   ├── terraform/ (or cloudformation/)
│   └── architecture.png
├── /docs
│   ├── technical-writeup.md
│   └── stakeholder-summary.md
├── /tests
│   └── test_handler.py
└── README.md
```

---

## Success Criteria

By the end of this project, you'll have demonstrated:
- ✅ **Cloud Architecture:** Serverless design, AWS service integration
- ✅ **AI/ML Integration:** Practical LangChain application
- ✅ **Identity Management:** Active Directory automation
- ✅ **Security:** Proper credential management, least privilege
- ✅ **Business Communication:** Translating technical to business value
- ✅ **Decision Making:** Documented technical choices with reasoning

This project shows you can build production-ready systems that solve real business problems while following best practices.