/// Resolution target for the unqualified `##class(Helper).run()` in Sample.cls,
/// which reaches it through that file's `Import Erp.Util` rather than its own
/// package.
Class Erp.Util.Helper Extends %Library.RegisteredObject
{

ClassMethod run(code As %Library.String) As %Library.Status
{
	quit $$$OK
}

}
