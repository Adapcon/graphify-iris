Import Erp.Util

/// Cross-file resolution target for ##class(Erp.Sample).save().
Class Erp.Sample Extends %Library.Persistent
{

/// Unqualified element class: `Erp.SampleLine`, via this class's own package.
Property lines As list Of SampleLine;

ClassMethod save(code As %Library.String, status As %Library.String) As %Library.Status
{
	// Unqualified class name reached through `Import Erp.Util`.
	quit ##class(Helper).run(code)
}

}
