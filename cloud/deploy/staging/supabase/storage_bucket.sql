-- Create the private STAGING results bucket in the STAGING Supabase project.
-- Run once in the staging project's SQL editor (or via psql against the staging DB).
--
-- public = false: objects are never served without authorization. The API reads
-- them with S3 access keys and streams them only to the job's owner. There are no
-- storage.objects policies for anon/authenticated on this bucket, so neither the
-- browser nor Supabase's REST storage API can read it directly.

insert into storage.buckets (id, name, public, file_size_limit)
values ('careercloud-staging-results', 'careercloud-staging-results', false, 104857600)
on conflict (id) do update set public = false;
